"""Tiny front matter reader/writer for knowledge notes.

Deliberately a strict subset of YAML so that notes stay legible and no YAML
dependency is needed::

    ---
    title: Incus mesh
    hosts: [example-host, example-server]
    tags: [incus]
    safety: read
    updated: 2026-09-19
    ---

Values are either a bare string or a ``[a, b]`` list of bare strings.
"""

from __future__ import annotations

FIELDS_ORDER = ["title", "kind", "hosts", "tags", "tools", "approve", "safety", "updated", "sources"]
LIST_FIELDS = {"hosts", "tags", "tools", "approve", "sources"}
SAFETY = ("read", "change", "destructive")


class FrontMatterError(ValueError):
    pass


def parse(text: str) -> tuple[dict[str, str | list[str]], str]:
    """Split ``text`` into (metadata, body). Text without front matter has empty metadata."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        raise FrontMatterError("front matter is not closed with ---")
    header = text[4:end]
    rest = text[end + 4 :]
    body = rest[1:] if rest.startswith("\n") else rest
    meta: dict[str, str | list[str]] = {}
    for lineno, line in enumerate(header.splitlines(), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise FrontMatterError(f"line {lineno}: expected 'key: value'")
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [item.strip() for item in value[1:-1].split(",") if item.strip()]
        else:
            meta[key] = value
    return meta, body


def render(meta: dict[str, str | list[str]], body: str) -> str:
    keys = [k for k in FIELDS_ORDER if k in meta] + sorted(k for k in meta if k not in FIELDS_ORDER)
    lines = ["---"]
    for key in keys:
        value = meta[key]
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body.lstrip("\n")


def as_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def validate(meta: dict[str, str | list[str]]) -> list[str]:
    """Return a list of problems (empty when the note's metadata is valid)."""
    problems = []
    if not meta.get("title"):
        problems.append("missing title")
    if meta.get("safety", "read") not in SAFETY:
        problems.append(f"safety must be one of {', '.join(SAFETY)}")
    for key in LIST_FIELDS & meta.keys():
        if not isinstance(meta[key], list):
            problems.append(f"{key} must be a [list]")
    return problems
