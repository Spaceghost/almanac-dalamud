"""Planning, triage and summaries with the local model (free, around the clock).

The local model proposes a plan as JSON; ``normalize_plan`` then enforces the
rules no model output can override:

* only known step kinds; at most ``max_steps`` steps;
* ``code`` steps only for tasks with an allowed repository;
* every ``code`` step is followed by a ``test`` step, and a plan that changes
  code ends with ``pr`` (draft), ``review`` (a local model on another backend
  reviews the diff) and ``ci`` (must pass before done);
* ``game_read`` may name only tools the game server lists in a free tier;
  ``game_action`` becomes an approval ticket, never a direct call.

If the model is unavailable (guard refused, backend down, bad JSON) a
deterministic fallback plan is used, so work never depends on the model
being up.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from .store import STEP_KINDS, Task

PLAN_SYSTEM = """You plan work for an autonomous coding assistant. Reply with JSON only:
{"steps": [{"kind": "...", "title": "...", "args": {...}}], "notes": "..."}
Step kinds:
- local: think/summarize/triage/write docs text with the local model. args: {"prompt": "..."}
- code: a coding session (Claude Code or Codex) edits the repository. args: {"prompt": "precise instructions"}
- test: run the repository's own test command. args: {}
(test, pr, review and ci steps are added automatically after code steps.)
- game_read: read FFXIV state through a read-only game tool. args: {"tool": "name", "args": {...}}
- game_action: ask the player to approve ONE game action (never movement, combat, gathering or trading).
  args: {"tool": "name", "args": {...}, "reason": "why"}
Keep plans short (2-6 steps). Prefer one focused code step with a clear prompt.
Never plan pushes to main, deployments, or anything needing secrets."""


class Model(Protocol):
    def complete(self, system: str, user: str, json_mode: bool = False) -> str: ...


class ModelUnavailable(RuntimeError):
    pass


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in model output")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model output is not an object")
    return data


PR_TAIL: list[dict[str, Any]] = [
    {"kind": "pr", "title": "Open a draft pull request", "args": {}},
    {"kind": "review", "title": "Cross-review by a local model", "args": {"round": 1}},
    {"kind": "ci", "title": "Wait for CI", "args": {}},
]


def fallback_plan(task: Task, has_repo: bool) -> list[dict[str, Any]]:
    if not has_repo:
        return [{"kind": "local", "title": "Triage and summarize for the owner", "args": {"prompt": f"{task.title}\n\n{task.body}"}}]
    return [
        {"kind": "code", "title": "Implement", "args": {"prompt": f"{task.title}\n\n{task.body}"}},
        {"kind": "test", "title": "Run the repository's tests", "args": {}},
        *PR_TAIL,
    ]


def normalize_plan(
    steps: list[dict[str, Any]], task: Task, has_repo: bool, game_read_tools: set[str], game_action_tools: set[str], max_steps: int = 12
) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", "")).strip()
        args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
        title = str(raw.get("title") or kind)[:200]
        if kind not in STEP_KINDS or kind in ("pr", "ci", "test", "review"):
            continue  # test/pr/review/ci are placed by the rules below
        if kind == "code" and not has_repo:
            continue
        if kind == "code" and not str(args.get("prompt", "")).strip():
            args = {**args, "prompt": f"{task.title}\n\n{task.body}"}
        if kind == "game_read" and str(args.get("tool", "")) not in game_read_tools:
            continue
        if kind == "game_action":
            if str(args.get("tool", "")) not in game_action_tools:
                continue
            args = {"tool": str(args["tool"]), "args": args.get("args") if isinstance(args.get("args"), dict) else {}, "reason": str(args.get("reason", title))[:300]}
        clean.append({"kind": kind, "title": title, "args": args})
    out: list[dict[str, Any]] = []
    for step in clean:
        out.append(step)
        if step["kind"] == "code":
            out.append({"kind": "test", "title": "Run the repository's tests", "args": {}})
    if any(s["kind"] == "code" for s in out):
        out += PR_TAIL
    if not out:
        return fallback_plan(task, has_repo)
    if len(out) > max_steps:
        tail = [s for s in out if s["kind"] in ("pr", "review", "ci")]
        out = [s for s in out if s["kind"] not in ("pr", "review", "ci")][: max_steps - len(tail)] + tail
    return out


class Planner:
    def __init__(self, model: Model | None, max_steps: int = 12) -> None:
        self.model = model
        self.max_steps = max_steps

    def plan(
        self, task: Task, has_repo: bool, game_read_tools: set[str], game_action_tools: set[str], context: str = ""
    ) -> tuple[list[dict[str, Any]], str]:
        """Returns (steps, how) where how is 'model' or 'fallback: <reason>'."""
        if self.model is None:
            return fallback_plan(task, has_repo), "fallback: no local model configured"
        user = (
            f"Task #{task.id} from {task.source}: {task.title}\n\n{task.body[:6000]}\n\n"
            f"Repository: {task.repo or '(none: no code steps possible)'}\n"
            f"Game read tools available: {', '.join(sorted(game_read_tools)) or 'none'}\n"
            f"Game action tools (approval needed): {', '.join(sorted(game_action_tools)) or 'none'}\n"
            f"{context}"
        )
        try:
            data = extract_json(self.model.complete(PLAN_SYSTEM, user, json_mode=True))
            steps = data.get("steps")
            if not isinstance(steps, list):
                raise ValueError("no steps list")
        except (ModelUnavailable, ValueError, json.JSONDecodeError) as exc:
            return fallback_plan(task, has_repo), f"fallback: {exc}"
        return normalize_plan(steps, task, has_repo, game_read_tools, game_action_tools, self.max_steps), "model"

    def replan_after_denial(
        self, task: Task, denied: str, has_repo: bool, game_read_tools: set[str], game_action_tools: set[str]
    ) -> tuple[list[dict[str, Any]], str]:
        # A denied action is never re-requested in the new plan.
        context = f"\nThe player DENIED this game action; do not request it again: {denied}\n"
        steps, how = self.plan(task, has_repo, game_read_tools, game_action_tools, context)
        denied_tool = denied.split(" ", 1)[0]
        return [s for s in steps if not (s["kind"] == "game_action" and s["args"].get("tool") == denied_tool)], how

    def local(self, prompt: str, context: str = "") -> str:
        if self.model is None:
            raise ModelUnavailable("no local model configured")
        system = "You are a concise engineering assistant. Answer in short Markdown suitable for a task log."
        return self.model.complete(system, f"{context}\n\n{prompt}".strip())

    def summarize_pr(self, task: Task, diffstat: str, evidence: str, notes: str) -> str:
        fallback = pr_body(task, diffstat, evidence, notes, summary="")
        if self.model is None:
            return fallback
        try:
            summary = self.model.complete(
                "Summarize a code change for a draft pull request in 3-6 bullet points, then list remaining risks. Markdown only.",
                f"Task: {task.title}\n\n{task.body[:3000]}\n\nDiffstat:\n{diffstat[:3000]}\n\nSession notes:\n{notes[:4000]}",
            )
        except ModelUnavailable:
            return fallback
        return pr_body(task, diffstat, evidence, notes, summary=summary.strip())


def pr_body(task: Task, diffstat: str, evidence: str, notes: str, summary: str) -> str:
    return (
        f"Draft opened by almanac autopilot for task #{task.id} ({task.source}).\n\n"
        f"## Task\n\n{task.title}\n\n{task.body[:2000]}\n\n"
        f"## Summary and remaining risks\n\n{summary or '(local model unavailable; see session notes)'}\n\n"
        f"## Test evidence\n\n```\n{evidence[-3000:]}\n```\n\n"
        f"## Diffstat\n\n```\n{diffstat[:3000]}\n```\n\n"
        "This is a draft. CI must pass and a human must review before merging.\n"
    )


SPLIT_SYSTEM = """Split a coding task into at most {n} small, independent, sequential sub-tasks that a
small local coding model can each finish in one short session (one or two files each).
Reply with JSON only: {{"subtasks": ["precise instruction", ...]}}. If the task is already small, return one."""


def split_for_local(model: Model | None, prompt: str, max_parts: int = 3) -> list[str]:
    """Smaller scopes for local coders. Falls back to the prompt itself."""
    if model is None or max_parts <= 1:
        return [prompt]
    try:
        data = extract_json(model.complete(SPLIT_SYSTEM.format(n=max_parts), prompt[:6000], json_mode=True))
    except (ModelUnavailable, ValueError, json.JSONDecodeError):
        return [prompt]
    parts = [str(p).strip() for p in data.get("subtasks", []) if str(p).strip()] if isinstance(data.get("subtasks"), list) else []
    return parts[:max_parts] or [prompt]


REVIEW_SYSTEM = """You review a pull request diff written by another AI coding agent. Be specific and brief.
Reply with JSON only: {"verdict": "approve" | "changes", "summary": "one paragraph",
"comments": ["file:line - issue and fix", ...], "tests_to_add": ["behaviour that lacks a test", ...]}
Ask for changes only for real bugs, missing tests for new behaviour, or clear simplifications."""


def review_diff(model: Model, task: Task, diff: str, evidence: str) -> dict[str, Any]:
    raw = model.complete(
        REVIEW_SYSTEM,
        f"Task: {task.title}\n\n{task.body[:2000]}\n\nTest output (tail):\n{evidence[-2000:]}\n\nDiff:\n{diff[:20000]}",
        json_mode=True,
    )
    data = extract_json(raw)
    verdict = "changes" if str(data.get("verdict", "")).lower().startswith("change") else "approve"
    comments = [str(c) for c in data.get("comments", []) if str(c).strip()] if isinstance(data.get("comments"), list) else []
    tests = [str(t) for t in data.get("tests_to_add", []) if str(t).strip()] if isinstance(data.get("tests_to_add"), list) else []
    if verdict == "changes" and not (comments or tests):
        verdict = "approve"
    return {"verdict": verdict, "summary": str(data.get("summary", ""))[:2000], "comments": comments[:20], "tests_to_add": tests[:10]}
