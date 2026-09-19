"""Headless agent: the local model + the knowledge base + the declared tools.

``almanac ask``, ``almanac run <runbook>`` and ``almanac chat`` use this loop.
It talks OpenAI Chat Completions (streaming) directly to the local backend (no
cloud tokens) and runs almanac tools through ``Almanac.call``, so validation,
confirmation and the audit log are identical to the MCP path. Tools of
companion MCP servers (``[upstreams]``, e.g. XivMcp in game) are offered too,
and progress is posted to their status board when they have one.

What the model may use:

* always: kb_search, kb_read, kb_list, every ``read`` tool, and the companion
  servers' free tiers (XivMcp: read, ui);
* ``change`` tools only with ``allow_change`` and then only after approval:
  either the runbook lists the tool under ``approve:`` (the owner approved it
  by writing the runbook) or a human answers y at an interactive prompt;
* companion action/chat tiers only with ``allow_game_actions`` (the game still
  asks the player to confirm each one);
* ``destructive`` tools: never.

A runbook's ``tools:`` front matter narrows the almanac tools further.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from . import frontmatter
from .guard import Guard
from .service import Almanac
from .upstream import SEP, Companions

SYSTEM = """You are almanac, a careful operator for a small set of machines.
Hosts you may target: {hosts}. This machine is {this_host}.
Work through the tools. For questions about hosts, services or projects, first
kb_search (and kb_read) the knowledge base; the notes say where things live
and what is safe. Prefer read-only tools. If a tool result says NOT RUN, the
action was not approved: do not retry it, report the plan instead.
Tools named <server>__<tool> belong to companion servers (for example the
game). Only use their action/chat tools when the user asked for that action.
Answer concisely for a terminal about 100 columns wide: short paragraphs or
bullets, quote key output lines, say what needs a human. Never invent output."""

Approver = Callable[[str, str], bool]
TextSink = Callable[[str], None]
EventSink = Callable[[str, dict[str, Any]], None]


class GuardRefused(RuntimeError):
    """The resource guard declined to load a model (memory tight etc.)."""


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


def parse_sse_chat(lines: Iterator[str], on_text: TextSink | None) -> dict[str, Any]:
    """Fold an OpenAI chat.completion.chunk SSE stream into one assistant message."""
    content: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if chunk.get("error"):
            raise RuntimeError(str(chunk["error"]))
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content"):
                content.append(delta["content"])
                if on_text:
                    on_text(delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function", {})
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return message


class Agent:
    def __init__(
        self,
        almanac: Almanac,
        model: str | None = None,
        allow_change: bool = False,
        allowed_tools: list[str] | None = None,
        preapproved: list[str] | None = None,
        approver: Approver = tty_approver,
        post: Callable[[dict[str, Any], TextSink | None], dict[str, Any]] | None = None,
        companions: Companions | None = None,
        allow_game_actions: bool = False,
    ) -> None:
        self.almanac = almanac
        gateway = almanac.config.section("gateway")
        agent_cfg = almanac.config.section("agent")
        self.backend = str(gateway["backend"]).rstrip("/")
        self.model = model or agent_cfg.get("model") or gateway["default_model"]
        self.max_steps = int(agent_cfg.get("max_steps", 8))
        self.temperature = float(agent_cfg.get("temperature", 0.2))
        # OpenAI reasoning_effort; "none" turns thinking off (much faster on small models).
        self.reasoning = str(agent_cfg.get("reasoning_effort", "none"))
        self.allow_change = allow_change
        self.preapproved = set(preapproved or [])
        self.approver = approver
        self._post = post or self._http_post
        catalogue = almanac.catalogue("change" if allow_change else "read")
        always = {"kb_search", "kb_read", "kb_list"}
        if allowed_tools is not None:
            catalogue = [t for t in catalogue if t["name"] in always or t["name"] in allowed_tools]
        self.catalogue = [t for t in catalogue if t["name"] != "kb_note" or allow_change]
        if companions is None:
            companions = Companions(dict(almanac.config.raw.get("upstreams", {})), allow_actions=allow_game_actions)
            companions.discover()
        self.companions = companions
        self.messages: list[dict[str, Any]] = []

    # -- model -------------------------------------------------------------
    def _http_post(self, payload: dict[str, Any], on_text: TextSink | None) -> dict[str, Any]:
        loaded: list[str] = []
        try:
            loaded = [m["name"] for m in httpx.get(f"{self.backend}/api/ps", timeout=5).json().get("models", [])]
        except httpx.HTTPError:
            pass
        verdict = Guard(self.almanac.config.section("guard")).may_load(self.model in loaded)
        if not verdict.ok:
            raise GuardRefused(f"not running: {verdict.reason}")
        with httpx.stream("POST", f"{self.backend}/v1/chat/completions", json={**payload, "stream": True}, timeout=600) as response:
            if response.status_code >= 400:
                response.read()
                raise RuntimeError(f"model backend returned {response.status_code}: {response.text[:300]}")
            return parse_sse_chat(response.iter_lines(), on_text)

    def functions(self) -> list[dict[str, Any]]:
        out = []
        for spec in self.catalogue:
            schema = json.loads(json.dumps(spec["input_schema"]))
            schema.get("properties", {}).pop("confirm", None)
            out.append({"type": "function", "function": {"name": spec["name"], "description": spec["description"], "parameters": schema}})
        return out + self.companions.functions()

    # -- tools -------------------------------------------------------------
    def _call(self, name: str, args: dict[str, Any]) -> str:
        caller = f"agent:{self.model}"
        if SEP in name and name in self.companions.tools:
            self.almanac.audit("upstream", name, args, caller)
            return self.companions.call(name, args)
        if name not in {t["name"] for t in self.catalogue}:
            return f"error: tool {name} is not available in this run"
        outcome = self.almanac.call(name, args, caller=caller)
        if outcome.needs_confirmation:
            if name in self.preapproved or self.approver(name, outcome.plan):
                outcome = self.almanac.call(name, args, caller=caller, approved=True)
            else:
                self.almanac.audit("declined", name, args, caller)
                return f"NOT RUN (not approved). Plan was:\n{outcome.plan}"
        return outcome.text

    # -- conversation ------------------------------------------------------
    def start(self, context: str = "") -> None:
        config = self.almanac.config
        system = SYSTEM.format(hosts=", ".join(config.hosts), this_host=config.this_host)
        if self.companions.notes:
            system += "\n\nCompanion servers: " + " ".join(self.companions.notes)
        if context:
            system += "\n\n" + context
        self.messages = [{"role": "system", "content": system}]

    def turn(self, user_text: str, on_text: TextSink | None = None, on_event: EventSink | None = None, transcript: Transcript | None = None) -> str:
        """One user message -> tool calls as needed -> final answer (streamed to on_text)."""
        if not self.messages:
            self.start()
        transcript = transcript or Transcript(user_text)
        self.messages.append({"role": "user", "content": user_text})
        for step in range(self.max_steps):
            message = self._post(
                {
                    "model": self.model, "messages": self.messages, "tools": self.functions(),
                    "temperature": self.temperature, "reasoning_effort": self.reasoning,
                },
                on_text,
            )
            calls = message.get("tool_calls") or []
            self.messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                transcript.answer = (message.get("content") or "").strip()
                return transcript.answer
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                args = args if isinstance(args, dict) else {}
                if on_event:
                    on_event("tool", {"name": name, "args": args, "step": step + 1})
                self.companions.post_status(f"step {step + 1}: {name}", "running", progress=min(0.9, (step + 1) / self.max_steps))
                result = self._call(name, args)
                if on_event:
                    on_event("result", {"name": name, "text": result})
                transcript.steps.append(f"## {name} {json.dumps(args)}\n\n```\n{result[:4000]}\n```")
                self.messages.append({"role": "tool", "tool_call_id": call.get("id") or name, "content": result})
        transcript.answer = "(stopped: step limit reached)"
        return transcript.answer

    def run(self, task: str, context: str = "", on_text: TextSink | None = None, on_event: EventSink | None = None) -> Transcript:
        """A one-shot task with progress on the companions' status boards."""
        transcript = Transcript(task)
        self.start(context)
        self.companions.post_status(f"started: {task[:150]}", "running", progress=0.0)
        try:
            self.turn(task, on_text, on_event, transcript)
        except Exception as exc:
            self.companions.post_status(f"failed: {task[:120]}", "failed", detail=str(exc))
            raise
        state = "failed" if transcript.answer.startswith("(stopped") else "done"
        self.companions.post_status(f"{state}: {task[:140]}", state, progress=1.0, detail=transcript.answer)
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
