"""The one place that decides whether a call runs: validation, confirmation, audit.

Both front ends (the MCP server and the local agent) go through
``Almanac.call``. The rules:

* ``read`` tools run immediately.
* ``change`` and ``destructive`` tools never run on the first call. The first
  call returns a *plan* (host, exact argv, safety class) and a confirmation
  token bound to exactly those arguments. Running requires either
  - the caller's own interactive approval (MCP elicitation, or a y/N prompt
    in the CLI), passed in as ``approved=True``, or
  - calling again with ``confirm=<token>`` (for clients without elicitation,
    after the model has shown the plan to the human).
* ``kb_note`` is a ``change``: its plan is the unified diff of the note.
* Every change/destructive decision (planned, approved, refused, ran) and
  every tool run is appended to ``<state_dir>/audit.jsonl``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .kb import KnowledgeBase, KnowledgeError, ollama_embedder
from .tools import Tool, ToolError, execute, load_tools

KB_TOOLS: dict[str, dict[str, Any]] = {
    "kb_search": {
        "safety": "read",
        "description": "Search the knowledge base (hosts, services, projects, runbooks). Returns note paths, titles and snippets. Use before acting on a host.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to search for."},
                "host": {"type": "string", "description": "Only notes about this host (or 'any')."},
                "tag": {"type": "string", "description": "Only notes with this tag."},
                "kind": {"type": "string", "enum": ["note", "runbook"], "description": "Only this kind of note."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 8},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "kb_read": {
        "safety": "read",
        "description": "Read one knowledge note in full (front matter + Markdown). Returns its sha256, needed to update it with kb_note.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Note path from kb_search, e.g. hosts/example-host.md"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "kb_list": {
        "safety": "read",
        "description": "List all knowledge notes (path, title, hosts, tags). kind='runbook' lists runbooks.",
        "schema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["note", "runbook"]}},
            "additionalProperties": False,
        },
    },
    "kb_note": {
        "safety": "change",
        "description": (
            "Add a new knowledge note or update an existing one. The first call returns the diff and a confirm token; "
            "nothing is written until the human approves. Updating requires base_sha256 from kb_read. "
            "Never put secrets, tokens or passwords in notes; reference where they live instead."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "topic/name.md, e.g. services/example-service.md"},
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Markdown body (no front matter)."},
                "hosts": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "safety": {"type": "string", "enum": ["read", "change", "destructive"], "default": "read"},
                "base_sha256": {"type": "string", "description": "sha256 from kb_read when updating."},
                "confirm": {"type": "string", "description": "Token from the plan, after the human approved it."},
            },
            "required": ["path", "title", "body"],
            "additionalProperties": False,
        },
    },
}

CONFIRM_SCHEMA = {
    "type": "string",
    "description": "Only for change/destructive tools: the token returned by the first call, after the human approved the plan.",
}


@dataclass
class Outcome:
    """Result of a call. ``needs_confirmation`` means nothing ran yet."""

    text: str
    is_error: bool = False
    needs_confirmation: bool = False
    plan: str = ""
    token: str = ""


class Almanac:
    def __init__(self, config: Config) -> None:
        self.config = config
        embed_model = config.section("kb").get("embed_model")
        embedder = ollama_embedder(config.section("gateway")["backend"], embed_model) if embed_model else None
        self.kb = KnowledgeBase(config.knowledge_dirs, config.state_dir / "index.sqlite", embedder)
        self.tools: dict[str, Tool] = {}
        known = list(config.hosts)
        for directory in config.tools_dirs:
            if directory.is_dir():
                self.tools.update(load_tools(directory, known))
        for name in config.raw.get("disabled_tools", []):
            self.tools.pop(name, None)
        self.audit_path = config.state_dir / "audit.jsonl"
        self._key = secrets.token_bytes(32)

    # -- catalogue ---------------------------------------------------------
    def catalogue(self, max_safety: str = "destructive") -> list[dict[str, Any]]:
        """Every callable tool as {name, description, safety, input_schema}."""
        order = ["read", "change", "destructive"]
        limit = order.index(max_safety)
        out = []
        for name, spec in KB_TOOLS.items():
            if order.index(spec["safety"]) <= limit:
                out.append({"name": name, "description": spec["description"], "safety": spec["safety"], "input_schema": spec["schema"]})
        for tool in self.tools.values():
            if order.index(tool.safety) > limit:
                continue
            schema = tool.input_schema()
            if tool.safety != "read":
                schema["properties"]["confirm"] = CONFIRM_SCHEMA
            hosts = "" if tool.run_on == "local" else f" Hosts: {', '.join(tool.hosts)}."
            out.append(
                {
                    "name": tool.name,
                    "description": f"[{tool.safety}] {tool.description}{hosts}",
                    "safety": tool.safety,
                    "input_schema": schema,
                }
            )
        return out

    def safety_of(self, name: str) -> str:
        if name in KB_TOOLS:
            return str(KB_TOOLS[name]["safety"])
        if name in self.tools:
            return self.tools[name].safety
        raise ToolError(f"unknown tool {name}")

    # -- confirmation + audit ------------------------------------------------
    def _token(self, name: str, args: dict[str, Any]) -> str:
        blob = json.dumps([name, args], sort_keys=True).encode()
        return hmac.new(self._key, blob, hashlib.sha256).hexdigest()[:16]

    def audit(self, event: str, name: str, args: dict[str, Any], caller: str, **extra: Any) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, "tool": name, "args": args, "caller": caller, **extra}
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def plan(self, name: str, args: dict[str, Any]) -> str:
        """Human-readable description of exactly what a call would do."""
        if name == "kb_note":
            preview = self._kb_note(args, write=False)
            return f"Write knowledge note {preview['path']} ({preview['action']}):\n\n{preview['diff'] or '(no change)'}"
        tool = self.tools[name]
        clean = tool.validate(args)
        target = tool.target_host(clean)
        host, commands = tool.render(clean, str(self.config.hosts.get(target, {}).get("init", "systemd")))
        lines = [f"Tool {name} [{tool.safety}] on {host} (timeout {tool.timeout}s):"]
        lines += [f"  $ {label}" for label, _ in commands]
        return "\n".join(lines)

    # -- dispatch ----------------------------------------------------------
    def call(self, name: str, args: dict[str, Any] | None, caller: str, approved: bool = False) -> Outcome:
        args = dict(args or {})
        confirm = args.pop("confirm", None)
        try:
            safety = self.safety_of(name)
            if safety != "read":
                plan = self.plan(name, args)
                token = self._token(name, args)
                if not approved and not (isinstance(confirm, str) and hmac.compare_digest(confirm, token)):
                    self.audit("planned", name, args, caller, safety=safety)
                    return Outcome(
                        text=(
                            f"NOT RUN: {safety} action needs the human's approval.\n{plan}\n\n"
                            f"Show this plan to the user. If they approve, call {name} again with the same arguments "
                            f'and confirm="{token}".'
                        ),
                        needs_confirmation=True,
                        plan=plan,
                        token=token,
                    )
                self.audit("approved", name, args, caller, safety=safety, via="interactive" if approved else "token")
            return self._run(name, args, caller)
        except (ToolError, KnowledgeError) as exc:
            if name not in ("kb_search", "kb_read", "kb_list"):
                self.audit("rejected", name, args, caller, error=str(exc))
            return Outcome(text=f"error: {exc}", is_error=True)

    def _kb_note(self, args: dict[str, Any], write: bool) -> dict[str, Any]:
        return self.kb.note(
            args["path"], args["title"], args["body"], hosts=args.get("hosts"), tags=args.get("tags"),
            safety=args.get("safety", "read"), base_sha256=args.get("base_sha256"), write=write,
        )

    def _run(self, name: str, args: dict[str, Any], caller: str) -> Outcome:
        if name == "kb_search":
            hits = self.kb.search(args["query"], args.get("host"), args.get("tag"), args.get("kind"), int(args.get("limit", 8)))
            lines = [
                f"{h['path']} | {h['title']} | {h['kind']} | hosts: {', '.join(h['hosts'])} | safety: {h['safety']}\n    {' '.join(h['snippet'].split())}"
                for h in hits
            ]
            return Outcome("\n".join(lines) + "\n(kb_read a path for the full note)" if hits else "no matching notes")
        if name == "kb_read":
            note = self.kb.load(args["path"])
            text = self.kb.resolve(note.path).read_text()
            return Outcome(f"path: {note.path}\nsha256: {note.sha256}\n\n{text}")
        if name == "kb_list":
            return Outcome("\n".join(f"{n['path']} | {n['title']} | hosts: {', '.join(n['hosts'])}" for n in self.kb.list(args.get("kind"))))
        if name == "kb_note":
            result = self._kb_note(args, write=True)
            self.audit("ran", name, {"path": result["path"]}, caller, written=result["written"])
            return Outcome(f"{'written' if result['written'] else 'unchanged'}: {result['path']}\n\n{result['diff']}")
        tool = self.tools[name]
        clean = tool.validate(args)
        result = execute(tool, clean, self.config.hosts, self.config.this_host)
        self.audit(
            "ran", name, clean, caller, safety=tool.safety, host=result.host,
            exit_codes=result.exit_codes, seconds=result.seconds, truncated=result.truncated,
        )
        header = f"host: {result.host}  exit: {','.join(map(str, result.exit_codes))}  seconds: {result.seconds}"
        if result.truncated:
            header += "  (output truncated)"
        return Outcome(f"{header}\n{result.output}", is_error=not result.ok)


def load(config_path: str | Path | None = None) -> Almanac:
    return Almanac(Config.load(config_path))
