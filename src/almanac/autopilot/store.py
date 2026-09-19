"""The persistent task queue (one SQLite file, WAL, safe to read while the loop runs).

A task has a plan (ordered steps), a state, a repository and branch, the budget
it has spent, blockers (approval tickets on its steps) and an event log.

Task states::

    queued       new, no plan yet
    ready        planned, has a pending step (may be delayed by next_at: backoff or budget)
    waiting      a step is parked on a game approval ticket; other tasks keep running
    review       draft PR open, waiting for CI
    needs_owner  stopped after too many failures, or needs a human decision
    done | cancelled

Step states: pending, running, done, failed, waiting (on a ticket), skipped.
"""

from __future__ import annotations

import hashlib
import json
import functools
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_ref TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  repo TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'queued',
  value REAL NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_at REAL NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  coding_runs INTEGER NOT NULL DEFAULT 0,
  pr_url TEXT NOT NULL DEFAULT '',
  authors TEXT NOT NULL DEFAULT '',
  worktree TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  idx INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  args TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'pending',
  result TEXT NOT NULL DEFAULT '',
  ticket_id TEXT NOT NULL DEFAULT '',
  resume_token TEXT NOT NULL DEFAULT '',
  updated REAL NOT NULL,
  UNIQUE(task_id, idx)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  task_id INTEGER,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  task_id INTEGER,
  coder TEXT NOT NULL,
  tokens INTEGER NOT NULL,
  cost REAL NOT NULL,
  seconds REAL NOT NULL,
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, ts);
CREATE INDEX IF NOT EXISTS usage_day ON usage(day);
"""

OPEN_STATES = ("queued", "ready", "waiting", "review", "needs_owner")
STEP_KINDS = ("local", "code", "test", "review", "game_read", "game_action", "pr", "ci")


@dataclass
class Step:
    id: int
    task_id: int
    idx: int
    kind: str
    title: str
    args: dict[str, Any]
    state: str
    result: str
    ticket_id: str
    resume_token: str


@dataclass
class Task:
    id: int
    source: str
    source_ref: str
    title: str
    body: str
    repo: str
    branch: str
    state: str
    value: float
    attempts: int
    next_at: float
    tokens: int
    cost: float
    coding_runs: int
    pr_url: str
    authors: str
    worktree: str
    note: str
    created: float
    updated: float
    steps: list[Step] = field(default_factory=list)

    def next_step(self) -> Step | None:
        for step in self.steps:
            if step.state in ("pending", "running", "waiting", "failed"):
                return step
        return None

    @property
    def blockers(self) -> list[str]:
        return [s.ticket_id for s in self.steps if s.state == "waiting" and s.ticket_id]


def locked(method: Any) -> Any:
    """Serialize access: the loop's worker threads share one connection."""

    @functools.wraps(method)
    def wrapper(self: "Store", *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper


def ref_for(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


class Store:
    def __init__(self, path: Path | str, clock: Any = time.time) -> None:
        self.path = str(path)
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # -- tasks -------------------------------------------------------------
    @locked
    def add_task(
        self, source: str, title: str, body: str = "", repo: str = "", value: float = 50.0, source_ref: str | None = None
    ) -> tuple[int, bool]:
        """Insert a task unless one with the same source_ref exists. Returns (id, created)."""
        ref = source_ref or f"{source}:{ref_for(title + body)}"
        row = self.db.execute("SELECT id FROM tasks WHERE source_ref = ?", (ref,)).fetchone()
        if row:
            return int(row["id"]), False
        now = self.clock()
        cur = self.db.execute(
            "INSERT INTO tasks (source, source_ref, title, body, repo, value, created, updated) VALUES (?,?,?,?,?,?,?,?)",
            (source, ref, title.strip()[:300], body, repo, float(value), now, now),
        )
        task_id = int(cur.lastrowid or 0)
        self.log(task_id, "added", f"from {source}: {title.strip()[:200]}")
        return task_id, True

    def _task(self, row: sqlite3.Row) -> Task:
        task = Task(**{k: row[k] for k in row.keys()})
        task.steps = self.steps(task.id)
        return task

    @locked
    def get(self, task_id: int) -> Task:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"no task {task_id}")
        return self._task(row)

    @locked
    def tasks(self, states: Iterable[str] | None = None) -> list[Task]:
        if states is None:
            rows = self.db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        else:
            states = list(states)
            marks = ",".join("?" * len(states))
            rows = self.db.execute(f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY id", states).fetchall()
        return [self._task(r) for r in rows]

    @locked
    def update(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated"] = self.clock()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))

    @locked
    def set_state(self, task_id: int, state: str, note: str = "", **fields: Any) -> None:
        self.update(task_id, state=state, note=note, **fields)
        self.log(task_id, "state", f"{state}{': ' + note if note else ''}")

    @locked
    def candidates(self, exclude: set[int] | None = None) -> list[Task]:
        """Runnable tasks, highest value first: not blocked on a ticket, not backing off, not in flight."""
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE state IN ('queued','ready','review') AND next_at <= ? ORDER BY value DESC, id ASC",
            (self.clock(),),
        ).fetchall()
        return [self._task(r) for r in rows if not exclude or r["id"] not in exclude]

    def pick(self, exclude: set[int] | None = None) -> Task | None:
        found = self.candidates(exclude)
        return found[0] if found else None

    def add_author(self, task_id: int, author: str) -> None:
        current = [a for a in self.get(task_id).authors.split(",") if a]
        if author not in current:
            self.update(task_id, authors=",".join(current + [author]))

    # -- steps -------------------------------------------------------------
    @locked
    def steps(self, task_id: int) -> list[Step]:
        rows = self.db.execute("SELECT * FROM steps WHERE task_id = ? ORDER BY idx", (task_id,)).fetchall()
        return [
            Step(
                id=r["id"], task_id=r["task_id"], idx=r["idx"], kind=r["kind"], title=r["title"], args=json.loads(r["args"]),
                state=r["state"], result=r["result"], ticket_id=r["ticket_id"], resume_token=r["resume_token"],
            )
            for r in rows
        ]

    @locked
    def set_plan(self, task_id: int, steps: list[dict[str, Any]]) -> None:
        """Replace the not-yet-done tail of the plan. Finished steps are kept (they are history)."""
        now = self.clock()
        kept = [s for s in self.steps(task_id) if s.state in ("done", "skipped")]
        self.db.execute("BEGIN")
        try:
            self.db.execute("DELETE FROM steps WHERE task_id = ? AND state NOT IN ('done','skipped')", (task_id,))
            start = (max(s.idx for s in kept) + 1) if kept else 0
            for offset, step in enumerate(steps):
                self.db.execute(
                    "INSERT INTO steps (task_id, idx, kind, title, args, updated) VALUES (?,?,?,?,?,?)",
                    (task_id, start + offset, step["kind"], str(step.get("title", step["kind"]))[:300], json.dumps(step.get("args", {})), now),
                )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        self.log(task_id, "plan", " -> ".join(f"{s['kind']}:{s.get('title', '')}"[:60] for s in steps))

    @locked
    def append_steps(self, task_id: int, steps: list[dict[str, Any]], after_idx: int) -> None:
        """Insert steps right after ``after_idx`` (shifting the rest)."""
        now = self.clock()
        n = len(steps)
        self.db.execute("BEGIN")
        try:
            # two-phase shift keeps UNIQUE(task_id, idx) satisfied
            self.db.execute("UPDATE steps SET idx = -idx - 1 WHERE task_id = ? AND idx > ?", (task_id, after_idx))
            self.db.execute("UPDATE steps SET idx = -idx - 1 + ? WHERE task_id = ? AND idx < 0", (n, task_id))
            for offset, step in enumerate(steps):
                self.db.execute(
                    "INSERT INTO steps (task_id, idx, kind, title, args, updated) VALUES (?,?,?,?,?,?)",
                    (task_id, after_idx + 1 + offset, step["kind"], str(step.get("title", step["kind"]))[:300], json.dumps(step.get("args", {})), now),
                )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    @locked
    def update_step(self, step_id: int, **fields: Any) -> None:
        if "args" in fields:
            fields["args"] = json.dumps(fields["args"])
        fields["updated"] = self.clock()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE steps SET {cols} WHERE id = ?", (*fields.values(), step_id))

    @locked
    def waiting_steps(self) -> list[Step]:
        rows = self.db.execute("SELECT task_id FROM steps WHERE state = 'waiting' AND ticket_id != ''").fetchall()
        out: list[Step] = []
        for task_id in sorted({r["task_id"] for r in rows}):
            out += [s for s in self.steps(task_id) if s.state == "waiting" and s.ticket_id]
        return out

    @locked
    def recover(self) -> int:
        """After a crash or kill: steps left 'running' go back to 'pending'."""
        cur = self.db.execute("UPDATE steps SET state = 'pending' WHERE state = 'running'")
        return cur.rowcount

    # -- log, usage, flags ---------------------------------------------------
    @locked
    def log(self, task_id: int | None, kind: str, message: str) -> None:
        self.db.execute("INSERT INTO events (task_id, ts, kind, message) VALUES (?,?,?,?)", (task_id, self.clock(), kind, message[:4000]))

    @locked
    def events(self, task_id: int | None = None, since: float = 0.0, limit: int = 500) -> list[dict[str, Any]]:
        if task_id is None:
            rows = self.db.execute("SELECT * FROM events WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, limit)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM events WHERE task_id = ? AND ts >= ? ORDER BY id DESC LIMIT ?", (task_id, since, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    @locked
    def record_usage(self, day: str, task_id: int, coder: str, tokens: int, cost: float, seconds: float) -> None:
        self.db.execute(
            "INSERT INTO usage (day, task_id, coder, tokens, cost, seconds, ts) VALUES (?,?,?,?,?,?,?)",
            (day, task_id, coder, int(tokens), float(cost), float(seconds), self.clock()),
        )
        self.db.execute(
            "UPDATE tasks SET tokens = tokens + ?, cost = cost + ?, coding_runs = coding_runs + 1 WHERE id = ?",
            (int(tokens), float(cost), task_id),
        )

    @locked
    def usage_for(self, day: str, kind: str = "cloud") -> dict[str, float]:
        """kind: cloud (claude/codex), local (coder names starting with 'local'), or all."""
        return self._usage("day = ?", (day,), kind)

    @locked
    def usage_since(self, since: float, kind: str = "all") -> dict[str, float]:
        return self._usage("ts >= ?", (since,), kind)

    def _usage(self, where: str, params: tuple[Any, ...], kind: str) -> dict[str, float]:
        if kind == "cloud":
            where += " AND coder NOT LIKE 'local%'"
        elif kind == "local":
            where += " AND coder LIKE 'local%'"
        row = self.db.execute(
            f"SELECT COUNT(*) AS runs, COALESCE(SUM(tokens),0) AS tokens, COALESCE(SUM(cost),0) AS cost FROM usage WHERE {where}", params
        ).fetchone()
        return {"runs": int(row["runs"]), "tokens": int(row["tokens"]), "cost": float(row["cost"])}

    @locked
    def flag(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM flags WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    @locked
    def set_flag(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO flags (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    @locked
    def close(self) -> None:
        self.db.close()
