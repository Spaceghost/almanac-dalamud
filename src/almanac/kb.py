"""The knowledge base: Markdown files are the truth, SQLite FTS5 is a cache.

* Every ``*.md`` under the configured knowledge roots is read, parsed (front
  matter + body) and indexed. Note ids are paths relative to their root
  (``hosts/example-host.md``); when two roots hold the same id the first
  root wins. New notes are written to the first root.
* The index lives in the state dir and is rebuilt whenever a note's mtime or
  the set of notes changes, so editing a file by hand is always enough.
* Optional embeddings: if ``[kb] embed_model`` names an Ollama embedding model
  that is installed, ``almanac index --embed`` stores one vector per note and
  searches fuse FTS rank with cosine rank. Without it, search is FTS only.
* Writes (``note``) never silently overwrite: creating requires the path to be
  free, updating requires the sha256 of the version you read, and every write
  returns a unified diff. A dry run (``propose``) returns the diff only.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import math
import re
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import frontmatter
from .secrets_scan import find_secrets

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(
  path TEXT PRIMARY KEY, mtime REAL, sha256 TEXT, title TEXT, kind TEXT,
  hosts TEXT, tags TEXT, safety TEXT, updated TEXT, body TEXT, embedding BLOB);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  path UNINDEXED, title, tags, hosts, body, tokenize='porter unicode61');
"""

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)*\.md$")

Embedder = Callable[[list[str]], list[list[float]]]


@dataclass
class Note:
    path: str
    meta: dict[str, Any]
    body: str
    sha256: str

    @property
    def title(self) -> str:
        return str(self.meta.get("title", self.path))


class KnowledgeError(ValueError):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _fts_query(query: str) -> str:
    words = re.findall(r"[\w.-]+", query.lower())
    return " OR ".join('"' + w.replace('"', "") + '"' for w in words if len(w) > 1)


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class KnowledgeBase:
    def __init__(self, roots: list[Path], index_path: Path, embedder: Embedder | None = None) -> None:
        if not roots:
            raise KnowledgeError("no knowledge directories configured")
        self.roots = roots
        self.root = roots[0]
        self.index_path = index_path
        self.embedder = embedder
        self._db: sqlite3.Connection | None = None

    # -- files -------------------------------------------------------------
    def files(self) -> dict[str, Path]:
        """Note id -> file, first root wins."""
        found: dict[str, Path] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.md")):
                rel = path.relative_to(root).as_posix()
                if any(part.startswith(".") for part in path.relative_to(root).parts):
                    continue
                found.setdefault(rel, path)
        return found

    def _rel(self, path: Path) -> str:
        for root in self.roots:
            if root.resolve() in path.resolve().parents:
                return path.resolve().relative_to(root.resolve()).as_posix()
        raise KnowledgeError(f"{path} is outside the knowledge roots")

    def resolve(self, rel: str, for_write: bool = False) -> Path:
        rel = rel.strip().removeprefix("knowledge/")
        if not NAME_RE.match(rel):
            raise KnowledgeError("note path must look like topic/name.md (lowercase, digits, dashes)")
        roots = self.roots[:1] if for_write else self.roots
        for root in roots:
            path = (root / rel).resolve()
            if root.resolve() not in path.parents:
                raise KnowledgeError("note path escapes the knowledge directory")
            if path.exists() or for_write:
                return path
        return (self.root / rel).resolve()

    def load(self, rel: str) -> Note:
        path = self.resolve(rel)
        if not path.is_file():
            raise KnowledgeError(f"no such note: {rel}")
        text = path.read_text()
        meta, body = frontmatter.parse(text)
        return Note(self._rel(path), meta, body, _sha(text))

    # -- index -------------------------------------------------------------
    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.index_path, check_same_thread=False)
            self._db.executescript(SCHEMA)
        return self._db

    def refresh(self, embed: bool = False) -> dict[str, int]:
        """Bring the index in line with the files. Cheap when nothing changed."""
        db = self.db
        known = {row[0]: row[1] for row in db.execute("SELECT path, mtime FROM notes")}
        seen: set[str] = set()
        changed = 0
        for rel, path in self.files().items():
            seen.add(rel)
            mtime = path.stat().st_mtime
            if known.get(rel) == mtime and not embed:
                continue
            text = path.read_text()
            try:
                meta, body = frontmatter.parse(text)
            except frontmatter.FrontMatterError:
                meta, body = {}, text
            fields = (
                str(meta.get("title", rel)),
                str(meta.get("kind", "note")),
                " ".join(frontmatter.as_list(meta.get("hosts"))),
                " ".join(frontmatter.as_list(meta.get("tags"))),
                str(meta.get("safety", "read")),
                str(meta.get("updated", "")),
            )
            vector = None
            if embed and self.embedder is not None:
                vector = _pack(self.embedder([f"{fields[0]}\n{body[:4000]}"])[0])
            db.execute("DELETE FROM notes WHERE path=?", (rel,))
            db.execute("DELETE FROM notes_fts WHERE path=?", (rel,))
            db.execute(
                "INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (rel, mtime, _sha(text), *fields, body, vector),
            )
            db.execute(
                "INSERT INTO notes_fts VALUES(?,?,?,?,?)", (rel, fields[0], fields[3], fields[2], body)
            )
            changed += 1
        removed = set(known) - seen
        for rel in removed:
            db.execute("DELETE FROM notes WHERE path=?", (rel,))
            db.execute("DELETE FROM notes_fts WHERE path=?", (rel,))
        db.commit()
        return {"notes": len(seen), "changed": changed, "removed": len(removed)}

    def search(
        self, query: str, host: str | None = None, tag: str | None = None, kind: str | None = None, limit: int = 8
    ) -> list[dict[str, Any]]:
        self.refresh()
        where, params = [], []
        if host:
            where.append("((' ' || n.hosts || ' ') LIKE ? OR (' ' || n.hosts || ' ') LIKE '% any %')")
            params.append(f"% {host} %")
        if tag:
            where.append("(' ' || n.tags || ' ') LIKE ?")
            params.append(f"% {tag} %")
        if kind:
            where.append("n.kind = ?")
            params.append(kind)
        extra = (" AND " + " AND ".join(where)) if where else ""
        fts = _fts_query(query)
        ranked: dict[str, float] = {}
        rows: dict[str, tuple[Any, ...]] = {}
        if fts:
            sql = (
                "SELECT n.path, n.title, n.kind, n.hosts, n.tags, n.safety, n.updated,"
                " snippet(notes_fts, 4, '[', ']', ' ... ', 24), bm25(notes_fts, 0, 8.0, 4.0, 2.0, 1.0)"
                " FROM notes_fts JOIN notes n ON n.path = notes_fts.path"
                f" WHERE notes_fts MATCH ?{extra} ORDER BY 9 LIMIT ?"
            )
            for rank, row in enumerate(self.db.execute(sql, (fts, *params, limit * 2))):
                rows[row[0]] = row
                ranked[row[0]] = ranked.get(row[0], 0.0) + 1.0 / (60 + rank)
        if self.embedder is not None and query.strip():
            vectors = list(self.db.execute(f"SELECT n.path, n.embedding FROM notes n WHERE n.embedding IS NOT NULL{extra}", params))
            if vectors:
                qv = self.embedder([query])[0]
                scored = sorted(vectors, key=lambda r: -_cosine(qv, _unpack(r[1])))[: limit * 2]
                for rank, (path, _) in enumerate(scored):
                    ranked[path] = ranked.get(path, 0.0) + 1.0 / (60 + rank)
        results = []
        for path in sorted(ranked, key=lambda p: -ranked[p])[:limit]:
            row = rows.get(path) or self.db.execute(
                "SELECT path, title, kind, hosts, tags, safety, updated, substr(body, 1, 200), 0 FROM notes WHERE path=?",
                (path,),
            ).fetchone()
            results.append(
                {
                    "path": row[0], "title": row[1], "kind": row[2], "hosts": row[3].split(),
                    "tags": row[4].split(), "safety": row[5], "updated": row[6], "snippet": row[7],
                }
            )
        return results

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        self.refresh()
        sql = "SELECT path, title, kind, hosts, tags FROM notes" + (" WHERE kind=?" if kind else "") + " ORDER BY path"
        return [
            {"path": p, "title": t, "kind": k, "hosts": h.split(), "tags": g.split()}
            for p, t, k, h, g in self.db.execute(sql, (kind,) if kind else ())
        ]

    # -- writes ------------------------------------------------------------
    def note(
        self,
        rel: str,
        title: str,
        body: str,
        hosts: list[str] | None = None,
        tags: list[str] | None = None,
        safety: str = "read",
        base_sha256: str | None = None,
        write: bool = False,
        today: dt.date | None = None,
    ) -> dict[str, Any]:
        """Create or update a note. Returns the diff; writes only if ``write``.

        Updating an existing note requires ``base_sha256`` equal to the sha256
        of the current file (as returned by read), so a change made by someone
        else in between is never overwritten.
        """
        path = self.resolve(rel, for_write=True)
        hits = find_secrets(title + "\n" + body)
        if hits:
            raise KnowledgeError("refusing to store what looks like a secret: " + "; ".join(hits))
        old = path.read_text() if path.exists() else ""
        old_meta: dict[str, Any] = frontmatter.parse(old)[0] if old else {}
        if old and base_sha256 != _sha(old):
            raise KnowledgeError(
                "note exists and base_sha256 does not match the current file; kb_read it first and pass its sha256"
            )
        meta: dict[str, Any] = dict(old_meta)
        meta.update(
            {
                "title": title.strip(),
                "hosts": hosts or frontmatter.as_list(old_meta.get("hosts")) or ["any"],
                "tags": tags or frontmatter.as_list(old_meta.get("tags")),
                "safety": safety,
                "updated": (today or dt.date.today()).isoformat(),
            }
        )
        problems = frontmatter.validate(meta)
        if problems:
            raise KnowledgeError("; ".join(problems))
        new = frontmatter.render(meta, body.rstrip() + "\n")
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/knowledge/{self._rel(path)}" if old else "/dev/null",
                tofile=f"b/knowledge/{self._rel(path)}",
            )
        )
        result = {"path": self._rel(path), "action": "update" if old else "create", "diff": diff, "written": False}
        if write and diff:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(new)
            tmp.replace(path)
            result.update(written=True, sha256=_sha(new))
            self.refresh()
        return result


def ollama_embedder(backend: str, model: str) -> Embedder:
    """Embedder using Ollama's /api/embed. Only built when a model is configured."""
    import httpx

    def embed(texts: list[str]) -> list[list[float]]:
        response = httpx.post(f"{backend}/api/embed", json={"model": model, "input": texts, "keep_alive": "1m"}, timeout=120)
        response.raise_for_status()
        return json.loads(response.text)["embeddings"]

    return embed
