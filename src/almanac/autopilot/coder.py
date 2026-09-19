"""Cloud coding sessions: Claude Code (``claude -p``) or Codex (``codex exec``), headless.

Each session runs in the task's worktree, with hard per-run limits (max turns
for Claude Code, wall time for both, enforced by the process runner), a
minimal environment and, when available, bubblewrap. Usage (tokens, cost) is
parsed from the CLI's JSON output and charged to the daily budget.

The session is told to commit but never push; autopilot pushes the branch
itself after the tests pass, and never to a protected branch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sandbox import ProcResult
from .settings import Repo

SESSION_RULES = """You are running unattended for almanac autopilot, in a git worktree of {repo}
on branch {branch}. Rules:
- Work only inside this directory. Do not push, do not open pull requests, do not use sudo,
  do not deploy, do not ssh anywhere{ssh_note}.
- Make the smallest change that completes the task. Commit your work with clear messages.
- Run the tests with: {test}  (they must pass before you finish).
- Never print, log or commit secrets or tokens.
- Finish with a short summary: what changed, test result, remaining risks.

Task:
{prompt}
"""

DENY_ALWAYS = ["Bash(sudo:*)", "Bash(su:*)", "Bash(git push:*)", "Bash(gh:*)", "Bash(doas:*)", "Bash(pkexec:*)", "Bash(op:*)"]
DENY_SSH = ["Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)", "Bash(sftp:*)"]
ALLOW_DEFAULT = ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "TodoWrite"]


@dataclass
class CodeResult:
    ok: bool
    tokens: int
    cost: float
    turns: int
    summary: str
    stopped: str
    seconds: float


def session_prompt(repo: Repo, branch: str, prompt: str) -> str:
    ssh_note = f" except {', '.join(repo.allow_ssh_hosts)}" if repo.allow_ssh_hosts else ""
    test = " ".join(repo.test) or "(no test command configured; do not invent one)"
    return SESSION_RULES.format(repo=repo.name, branch=branch, ssh_note=ssh_note, test=test, prompt=prompt)


def claude_argv(settings: dict[str, Any], repo: Repo, prompt: str, max_turns: int) -> list[str]:
    argv = [*settings.get("argv", ["claude"]), "-p", prompt, "--output-format", "json", "--max-turns", str(max_turns)]
    argv += ["--permission-mode", str(settings.get("permission_mode", "acceptEdits"))]
    argv += ["--allowedTools", ",".join(settings.get("allowed_tools", ALLOW_DEFAULT))]
    deny = DENY_ALWAYS + ([] if repo.allow_ssh_hosts else DENY_SSH) + list(settings.get("disallowed_tools", []))
    argv += ["--disallowedTools", ",".join(deny)]
    if settings.get("model"):
        argv += ["--model", str(settings["model"])]
    return argv + [str(a) for a in settings.get("extra_args", [])]


def codex_argv(settings: dict[str, Any], repo: Repo, worktree: Path, prompt: str) -> list[str]:
    argv = [*settings.get("argv", ["codex"]), "exec", "--json", "--sandbox", "workspace-write", "-C", str(worktree)]
    argv += ["-c", f"sandbox_workspace_write.network_access={'true' if repo.allow_ssh_hosts else 'false'}"]
    if settings.get("model"):
        argv += ["-m", str(settings["model"])]
    return argv + [str(a) for a in settings.get("extra_args", [])] + [prompt]


def parse_claude(result: ProcResult) -> CodeResult:
    data: dict[str, Any] = {}
    for line in reversed(result.output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if not data:
        try:
            data = json.loads(result.output)
        except json.JSONDecodeError:
            data = {}
    usage = data.get("usage") or {}
    tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
    ok = result.ok and not data.get("is_error", not data)
    return CodeResult(
        ok=bool(ok), tokens=tokens, cost=float(data.get("total_cost_usd") or 0.0), turns=int(data.get("num_turns") or 0),
        summary=str(data.get("result") or result.output[-2000:]), stopped=result.stopped or ("" if ok else str(data.get("subtype", "error"))),
        seconds=result.seconds,
    )


def parse_codex(result: ProcResult, usd_per_mtok: float = 0.0) -> CodeResult:
    tokens = 0
    turns = 0
    last_message = ""
    failed = False
    for line in result.output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type", "")
        if kind == "turn.completed":
            turns += 1
            usage = event.get("usage") or {}
            tokens += int(usage.get("input_tokens") or 0) - int(usage.get("cached_input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        elif kind in ("turn.failed", "error"):
            failed = True
        elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
            last_message = str(event["item"].get("text") or "")
    ok = result.ok and not failed
    return CodeResult(
        ok=ok, tokens=max(0, tokens), cost=tokens / 1_000_000 * usd_per_mtok, turns=turns,
        summary=last_message or result.output[-2000:], stopped=result.stopped or ("" if ok else "error"), seconds=result.seconds,
    )


# -- cloud limits ------------------------------------------------------------
# Output shapes of Claude Code / Codex / the Anthropic and OpenAI APIs when the
# account cannot continue. Matched case-insensitively against the session output.
LIMIT_PATTERNS: list[tuple[str, str]] = [
    ("auth", r"authentication_error|invalid (x-)?api[ -]key|invalid_api_key|401 unauthorized|\"status\":\s*401|please run /login|not logged in|oauth token (has )?expired"),
    ("quota", r"insufficient_quota|exceeded your current quota|credit balance is too low|payment required|\"status\":\s*402"),
    ("usage_limit", r"usage limit|you've hit your usage limit|limit will reset|weekly limit|5-hour limit"),
    ("rate_limit", r"rate_limit_error|rate limit(ed)?|too many requests|\"status\":\s*429|\b429\b|overloaded_error|\"status\":\s*529"),
]


def classify_limit(result: ProcResult, parsed: CodeResult | None = None) -> str:
    """'' for ordinary failures (or success); otherwise auth | quota | usage_limit | rate_limit.

    Only a failed run is classified: a successful session whose summary merely
    mentions "rate limit" is not a limit. Timeouts and the kill switch are not limits.
    """
    if result.ok and (parsed is None or parsed.ok):
        return ""
    if result.stopped:
        return ""
    text = result.output[-20000:].lower()
    for kind, pattern in LIMIT_PATTERNS:
        if re.search(pattern, text):
            return kind
    return ""


def limit_backoff_seconds(kind: str, now: float, next_midnight: float) -> float:
    """How long cloud coding stays off after a limit: rate limits briefly, the rest until the day resets."""
    if kind == "rate_limit":
        return 30 * 60
    if kind == "auth":
        return max(3600.0, next_midnight - now)  # needs the owner; re-tried daily
    return max(600.0, next_midnight - now)


# -- local coders ------------------------------------------------------------
LOCAL_PRESETS: dict[str, list[str]] = {
    # aider: one instruction, auto-commit; runs the tests itself when a command is known
    "aider": ["aider", "--model", "openai/{model}", "--yes-always", "--no-check-update", "--no-show-model-warnings",
              "--no-pretty", "--no-stream", "--auto-commits", "--no-gitignore", "--message", "{prompt}"],
    # Codex CLI pointed at an OpenAI-compatible local endpoint (almanac gateway or Ollama)
    "codex": ["codex", "exec", "--json", "--sandbox", "workspace-write", "-C", "{worktree}",
              "-c", 'model_provider="autopilot_local"',
              "-c", 'model_providers.autopilot_local.name="autopilot local"',
              "-c", 'model_providers.autopilot_local.base_url="{base_url}"',
              "-c", 'model_providers.autopilot_local.env_key="AUTOPILOT_LOCAL_KEY"',
              "-c", 'model_providers.autopilot_local.wire_api="responses"',
              "-m", "{model}", "{prompt}"],
}


def local_coder_argv(settings: dict[str, Any], repo: Repo, worktree: Path, base_url: str, model: str, prompt: str) -> list[str]:
    tool = str(settings.get("tool", "aider"))
    custom = settings.get("argv")
    template = [str(a) for a in (custom or LOCAL_PRESETS.get(tool, LOCAL_PRESETS["aider"]))]
    values = {"model": model, "base_url": base_url, "worktree": str(worktree), "prompt": prompt, "test": " ".join(repo.test)}
    argv = list(template)
    for key, value in values.items():
        argv = [part.replace("{" + key + "}", value) for part in argv]
    if tool == "aider" and repo.test and not custom:
        argv[-2:-2] = ["--test-cmd", " ".join(repo.test), "--auto-test"]
    return argv + [str(a) for a in settings.get("extra_args", [])]


def local_coder_env(base_url: str, token: str) -> dict[str, str]:
    key = token or "local"
    return {"OPENAI_API_BASE": base_url, "OPENAI_BASE_URL": base_url, "OPENAI_API_KEY": key, "AUTOPILOT_LOCAL_KEY": key}


def parse_local(tool: str, result: ProcResult) -> CodeResult:
    if tool == "codex":
        parsed = parse_codex(result)
        return CodeResult(parsed.ok, parsed.tokens, 0.0, parsed.turns, parsed.summary, parsed.stopped, parsed.seconds)
    ok = result.ok
    return CodeResult(ok, 0, 0.0, 1, result.output[-3000:], result.stopped or ("" if ok else f"exit {result.code}"), result.seconds)
