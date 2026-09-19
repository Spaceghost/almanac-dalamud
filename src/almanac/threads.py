"""Conversation threads: ``almanac ask --thread``, ``almanac chat --thread``.

A thread is the message history of one conversation with the local agent,
kept as JSON under ``<state_dir>/threads/<id>.json`` so a follow-up question
("and the other one?") reaches the model with what was said before. Only the
conversation is stored (user, assistant and tool messages); the system prompt
and the tool list are rebuilt on every turn, so a thread survives changes to
the knowledge base, the tools and the configuration.

What is sent back is trimmed to the model's context (``[agent]
context_tokens``, about four characters a token): whole turns, newest first,
each starting at a user message so a tool call is never separated from its
result. Old tool output is shortened further; the file keeps everything.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TITLE_MAX = 80
# Tool output older than the newest turn is cut to this many characters when replayed.
OLD_TOOL_CHARS = 1500


class ThreadError(ValueError):
    """A bad thread id, or a thread that does not exist."""


def valid_id(thread_id: str) -> bool:
    return bool(ID_RE.match(thread_id)) and ".." not in thread_id


def new_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def title_of(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_MAX else line[: TITLE_MAX - 1] + "…"


@dataclass
class Thread:
    id: str
    title: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    model: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)

    def turns(self) -> int:
        return sum(1 for m in self.messages if m.get("role") == "user")

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "created": self.created, "updated": self.updated,
                "model": self.model, "turns": self.turns()}

    def add_turn(self, messages: list[dict[str, Any]], model: str) -> None:
        """Append one turn's messages (user message first) and stamp it."""
        if not self.title:
            first = next((m for m in messages if m.get("role") == "user"), None)
            if first:
                self.title = title_of(str(first.get("content") or ""))
        self.messages += [clean_message(m) for m in messages]
        self.model = model or self.model
        self.updated = time.time()


def clean_message(message: dict[str, Any]) -> dict[str, Any]:
    """Only what the chat API needs again: role, content, tool_calls, tool_call_id."""
    return {k: v for k, v in message.items() if k in ("role", "content", "tool_calls", "tool_call_id") and v is not None}


def _turn_starts(messages: list[dict[str, Any]]) -> list[int]:
    return [i for i, m in enumerate(messages) if m.get("role") == "user"]


def _size(message: dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False))


def trim_history(messages: list[dict[str, Any]], budget_chars: int) -> list[dict[str, Any]]:
    """The newest whole turns of `messages` that fit in `budget_chars`.

    Tool results before the newest kept turn are shortened to OLD_TOOL_CHARS.
    The newest turn is kept even when it alone is over the budget (shortened).
    """
    starts = _turn_starts(messages)
    if not starts:
        return []
    kept: list[dict[str, Any]] = []
    used = 0
    ends = starts[1:] + [len(messages)]
    for index, (start, end) in enumerate(reversed(list(zip(starts, ends)))):
        turn = []
        for message in messages[start:end]:
            message = dict(message)
            if message.get("role") == "tool" and len(str(message.get("content") or "")) > OLD_TOOL_CHARS:
                message["content"] = str(message["content"])[:OLD_TOOL_CHARS] + "\n…(cut)"
            turn.append(message)
        size = sum(_size(m) for m in turn)
        if index > 0 and used + size > budget_chars:
            break
        kept = turn + kept
        used += size
    return kept


class ThreadStore:
    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir) / "threads"

    def _path(self, thread_id: str) -> Path:
        if not valid_id(thread_id):
            raise ThreadError(f"not a thread id: {thread_id!r} (letters, digits, '.', '_', '-'; at most 64)")
        return self.dir / f"{thread_id}.json"

    def exists(self, thread_id: str) -> bool:
        return self._path(thread_id).is_file()

    def load(self, thread_id: str) -> Thread:
        path = self._path(thread_id)
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            raise ThreadError(f"no thread {thread_id}") from None
        except ValueError as exc:
            raise ThreadError(f"thread {thread_id} is not readable: {exc}") from None
        return Thread(id=thread_id, title=str(data.get("title", "")), created=float(data.get("created", 0)),
                      updated=float(data.get("updated", 0)), model=str(data.get("model", "")),
                      messages=[m for m in data.get("messages", []) if isinstance(m, dict)])

    def open(self, thread_id: str | None) -> Thread:
        """The thread `thread_id` (created when missing), or a new one for None."""
        if thread_id is None:
            thread_id = new_id()
            while self.exists(thread_id):
                thread_id = new_id()
        return self.load(thread_id) if self.exists(thread_id) else Thread(id=self._path(thread_id).stem)

    def save(self, thread: Thread) -> Path:
        path = self._path(thread.id)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(asdict(thread), handle, ensure_ascii=False)
        os.replace(tmp, path)
        return path

    def list(self) -> list[Thread]:
        threads = []
        for path in self.dir.glob("*.json") if self.dir.is_dir() else []:
            try:
                threads.append(self.load(path.stem))
            except ThreadError:
                continue
        return sorted(threads, key=lambda t: t.updated, reverse=True)

    def latest(self) -> Thread | None:
        threads = self.list()
        return threads[0] if threads else None

    def delete(self, thread_id: str) -> bool:
        path = self._path(thread_id)
        if not path.exists():
            return False
        path.unlink()
        return True
