"""Headless agent: the local model + the knowledge base + the declared tools.

``almanac ask "task"`` and ``almanac run <runbook>`` use this loop. It talks
OpenAI Chat Completions directly to the local backend (no cloud tokens) and
runs tools through ``Almanac.call`` so validation, confirmation and the audit
log are identical to the MCP path.

What the model may use:

* always: kb_search, kb_read, kb_list, and every ``read`` tool;
* ``change`` tools only with ``allow_change`` and then only after approval:
  either the runbook lists the tool under ``approve:`` (the owner approved it
  by writing the runbook) or a human answers y at an interactive prompt;
* ``destructive`` tools: never.

A runbook's ``tools:`` front matter narrows the set further.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from . import frontmatter
from .guard import Guard
from .service import Almanac

SYSTEM = """You are almanac, a careful operator for a small set of machines.
Hosts you may target: {hosts}. This machine is {this_host}.
Work only through the tools. First kb_search (and kb_read) for the hosts,
services or projects involved; the notes say where things live and what is safe.
Prefer read-only tools. If a tool result says NOT RUN, the action was not
approved: do not retry it, report the plan instead.
Finish with a short report: what you checked, what you found (quote the key
output lines), anything that needs a human. Do not invent output."""

Approver = Callable[[str, str], bool]


@dataclass
class Transcript:
    task: str
    steps: list[str] = field(default_factory=list)
    answer: str = ""

    def markdown(self) -> str:
        body = "\n\n".join(self.steps)
        return f"# Task\n\n{self.task}\n\n# Steps\n\n{body}\n\n# Answer\n\n{self.answer}\n"


def tty_approver(name: str, plan: str) -> bool:
    if not sys.stdin.isatty():
        return False
    print(f"\n{plan}\n", file=sys.stderr)
    return input(f"Run {name}? [y/N] ").strip().lower() == "y"


class Agent:
    def __init__(
        self,
        almanac: Almanac,
        model: str | None = None,
        allow_change: bool = False,
        allowed_tools: list[str] | None = None,
        preapproved: list[str] | None = None,
        approver: Approver = tty_approver,
        post: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.almanac = almanac
        gateway = almanac.config.section("gateway")
        agent_cfg = almanac.config.section("agent")
        self.backend = str(gateway["backend"]).rstrip("/")
        self.model = model or agent_cfg.get("model") or gateway["default_model"]
        self.max_steps = int(agent_cfg.get("max_steps", 8))
        self.temperature = float(agent_cfg.get("temperature", 0.2))
        self.allow_change = allow_change
        self.preapproved = set(preapproved or [])
        self.approver = approver
        self._post = post or self._http_post
        catalogue = almanac.catalogue("change" if allow_change else "read")
        always = {"kb_search", "kb_read", "kb_list"}
        if allowed_tools is not None:
            catalogue = [t for t in catalogue if t["name"] in always or t["name"] in allowed_tools]
        self.catalogue = [t for t in catalogue if t["name"] != "kb_note" or allow_change]

    def _http_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        loaded = []
        try:
            loaded = [m["name"] for m in httpx.get(f"{self.backend}/api/ps", timeout=5).json().get("models", [])]
        except httpx.HTTPError:
            pass
        verdict = Guard(self.almanac.config.section("guard")).may_load(self.model in loaded)
        if not verdict.ok:
            raise RuntimeError(f"not running: {verdict.reason}")
        response = httpx.post(f"{self.backend}/v1/chat/completions", json=payload, timeout=600)
        response.raise_for_status()
        return response.json()

    def functions(self) -> list[dict[str, Any]]:
        out = []
        for spec in self.catalogue:
            schema = json.loads(json.dumps(spec["input_schema"]))
            schema.get("properties", {}).pop("confirm", None)
            out.append({"type": "function", "function": {"name": spec["name"], "description": spec["description"], "parameters": schema}})
        return out

    def _call(self, name: str, args: dict[str, Any]) -> str:
        if name not in {t["name"] for t in self.catalogue}:
            return f"error: tool {name} is not available in this run"
        outcome = self.almanac.call(name, args, caller=f"agent:{self.model}")
        if outcome.needs_confirmation:
            if name in self.preapproved or self.approver(name, outcome.plan):
                outcome = self.almanac.call(name, args, caller=f"agent:{self.model}", approved=True)
            else:
                self.almanac.audit("declined", name, args, f"agent:{self.model}")
                return f"NOT RUN (not approved). Plan was:\n{outcome.plan}"
        return outcome.text

    def run(self, task: str, context: str = "") -> Transcript:
        config = self.almanac.config
        system = SYSTEM.format(hosts=", ".join(config.hosts), this_host=config.this_host)
        if context:
            system += "\n\n" + context
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        transcript = Transcript(task)
        for _ in range(self.max_steps):
            reply = self._post(
                {"model": self.model, "messages": messages, "tools": self.functions(), "temperature": self.temperature, "stream": False}
            )
            message = reply["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                transcript.answer = (message.get("content") or "").strip()
                return transcript
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._call(name, args if isinstance(args, dict) else {})
                transcript.steps.append(f"## {name} {json.dumps(args)}\n\n```\n{result[:4000]}\n```")
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": result})
        transcript.answer = "(stopped: step limit reached)"
        return transcript


def load_runbook(almanac: Almanac, name: str) -> tuple[dict[str, Any], str]:
    rel = name if name.endswith(".md") else f"runbooks/{name}.md"
    note = almanac.kb.load(rel)
    if note.meta.get("kind") != "runbook":
        raise ValueError(f"{rel} is not a runbook (front matter kind: runbook)")
    return note.meta, f"Runbook {rel}: {note.title}\n\n{note.body}"


def save_run(almanac: Almanac, label: str, transcript: Transcript) -> Path:
    runs = almanac.config.state_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}.md"
    path.write_text(transcript.markdown())
    return path


def runbook_agent(almanac: Almanac, name: str, allow_change: bool, **kwargs: Any) -> tuple[Agent, str]:
    meta, text = load_runbook(almanac, name)
    tools = frontmatter.as_list(meta.get("tools")) or None
    approve = frontmatter.as_list(meta.get("approve"))
    agent = Agent(almanac, allow_change=allow_change or bool(approve), allowed_tools=tools, preapproved=approve, **kwargs)
    return agent, text
