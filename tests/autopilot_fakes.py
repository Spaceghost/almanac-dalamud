"""Fakes for the autopilot tests: no network, no real claude/codex/aider/gh/git pushes, no game."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from almanac.autopilot.game import Ticket
from almanac.autopilot.planner import ModelUnavailable, Planner
from almanac.autopilot.pool import Backend, Pool
from almanac.autopilot.runner import Autopilot, InlineExecutor
from almanac.autopilot.sandbox import ProcResult
from almanac.autopilot.settings import DEFAULTS, Repo, Settings, _merge
from almanac.autopilot.store import Store


class Clock:
    def __init__(self) -> None:
        # 03:00 local time: before the digest hour
        self.t = time.mktime((2026, 9, 19, 3, 0, 0, 0, 0, -1))

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeModel:
    """Returns scripted replies in order, then ``default``. ``None`` in the script = unavailable."""

    def __init__(self, replies: list[str | None] | None = None, default: str = "ok") -> None:
        self.replies = list(replies or [])
        self.default = default
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        self.prompts.append((system, user))
        if self.replies:
            reply = self.replies.pop(0)
            if reply is None:
                raise ModelUnavailable("scripted outage")
            return reply
        return self.default


class FakeGame:
    def __init__(self, action_tools: set[str] | None = None, read_tools: set[str] | None = None, objectives: bool = True) -> None:
        self.online = True
        self._actions = action_tools or set()
        self._reads = read_tools or set()
        self.tickets: dict[str, Ticket] = {}
        self.requests: list[dict[str, Any]] = []
        self.statuses: list[str] = []
        self.toasts: list[str] = []
        self.objectives: list[list[str]] = []
        self.has_objectives = objectives

    def refresh(self, force: bool = False) -> bool:
        return self.online

    def read_tools(self) -> set[str]:
        return set(self._reads)

    def action_tools(self) -> set[str]:
        return set(self._actions)

    def post_status(self, status: str, state: str = "running", progress: float | None = None, detail: str | None = None) -> None:
        self.statuses.append(f"{state}: {status}")

    def toast(self, message: str) -> bool:
        self.toasts.append(message)
        return True

    def set_objectives(self, items: list[str]) -> bool:
        if not self.has_objectives:
            return False
        self.objectives.append(list(items))
        return True

    def read(self, tool: str, args: dict[str, Any]) -> str:
        return f"{tool} says hi"

    def request_action(self, tool: str, args: dict[str, Any], reason: str, resume_token: str) -> Ticket:
        ticket_id = f"t{len(self.requests) + 1}"
        self.requests.append({"tool": tool, "args": args, "reason": reason, "resume_token": resume_token, "id": ticket_id})
        self.tickets[ticket_id] = Ticket(ticket_id, "pending")
        return self.tickets[ticket_id]

    def get_ticket(self, ticket_id: str) -> Ticket:
        return self.tickets[ticket_id]

    def cancel_ticket(self, ticket_id: str) -> None:
        self.tickets[ticket_id] = Ticket(ticket_id, "cancelled")


CLAUDE_OK = json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 7, "result": "Changed foo.py; tests pass.",
    "total_cost_usd": 0.42, "usage": {"input_tokens": 1000, "output_tokens": 500, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 99999},
})
CLAUDE_RATE_LIMITED = json.dumps({
    "type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 1,
    "result": 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Number of request tokens has exceeded your per-minute rate limit"}}',
    "total_cost_usd": 0.0, "usage": {"input_tokens": 10, "output_tokens": 0},
})


class FakeProc:
    """Stands in for every subprocess: git, gh, claude, codex, aider, the repo's tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.tests: list[int] = []  # exit codes for successive test runs (default 0)
        self.claude: list[tuple[int, str]] = []  # (exit, output) for successive claude runs
        self.checks: list[str] = []  # buckets for successive `gh pr checks`
        self.local: list[tuple[int, str]] = []
        self.pr_exists = False
        self.stat = " foo.py | 3 ++-\n 1 file changed, 2 insertions(+), 1 deletion(-)\n"

    def argvs(self, prefix: str) -> list[list[str]]:
        return [c["argv"] for c in self.calls if " ".join(c["argv"]).startswith(prefix)]

    def ran(self, *words: str) -> list[list[str]]:
        """Calls whose argv contains all of ``words`` (``git -C <dir> push ...`` and friends)."""
        return [c["argv"] for c in self.calls if all(w in c["argv"] for w in words)]

    def __call__(
        self, argv: list[str], cwd: Path, env: dict[str, str], timeout: float, stdin: str | None = None,
        abort: Callable[[], str] | None = None,
    ) -> ProcResult:
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "stdin": stdin, "timeout": timeout})
        if argv[0] == "bwrap":  # the sandbox wrapper: dispatch on the command it wraps
            argv = argv[argv.index("--") + 1:]
        joined = " ".join(argv)
        if argv[0] == "git":
            if "worktree" in argv and "add" in argv:
                path = Path(argv[argv.index("-B") + 2])
                path.mkdir(parents=True, exist_ok=True)
                (path / ".git").write_text("gitdir: elsewhere\n")
                return ProcResult(0, "", 0.1)
            if "rev-list" in argv:
                return ProcResult(0, "2\n", 0.1)
            if "--stat" in argv:
                return ProcResult(0, self.stat, 0.1)
            if argv[3:4] == ["diff"]:
                return ProcResult(0, "diff --git a/foo.py b/foo.py\n+new line\n", 0.1)
            if "rev-parse" in argv:
                return ProcResult(0, str(cwd) + "/.git\n", 0.1)
            return ProcResult(0, "", 0.1)
        if argv[0] == "gh":
            if joined.startswith("gh pr view"):
                return ProcResult(0, json.dumps({"url": "https://github.com/o/demo/pull/7"}), 0.1) if self.pr_exists else ProcResult(1, "no pull requests found", 0.1)
            if joined.startswith("gh pr create"):
                self.pr_exists = True
                return ProcResult(0, "https://github.com/o/demo/pull/7\n", 0.1)
            if joined.startswith("gh pr checks"):
                bucket = self.checks.pop(0) if self.checks else "pass"
                return ProcResult(0, json.dumps([{"name": "ci", "bucket": bucket}]), 0.1)
            return ProcResult(0, "", 0.1)
        if argv[0] == "claude":
            code, out = self.claude.pop(0) if self.claude else (0, CLAUDE_OK)
            return ProcResult(code, out, 12.0)
        if argv[0] in ("aider", "codex"):
            code, out = self.local.pop(0) if self.local else (0, "Applied edit to foo.py\nCommit abc123")
            return ProcResult(code, out, 30.0)
        if argv == ["tests/run.sh"]:
            code = self.tests.pop(0) if self.tests else 0
            return ProcResult(code, "ok 12 tests" if code == 0 else "FAILED test_foo: expected 2 got 3", 3.0)
        return ProcResult(127, f"unexpected command {joined}", 0.0)


class Resp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status


def make_settings(tmp_path: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {
        "dry_run": False,
        "sandbox": {"mode": "none"},
        # The loop tests are about the loop: the approval gates have their own file
        # (test_autopilot_approvals.py), which turns them back on explicitly.
        "approval": {"require": []},
        "repos": {"demo": {"path": str(tmp_path / "repo"), "github": "o/demo", "test": ["tests/run.sh"]}},
        "caps": {"coding_runs_per_day": 4, "local_split_parts": 1, "max_parallel": 1},
        "backoff_base_seconds": 60,
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict) and key != "repos":
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    raw = _merge(DEFAULTS, base)
    state = tmp_path / "ap"
    state.mkdir(exist_ok=True)
    (tmp_path / "repo").mkdir(exist_ok=True)
    settings = Settings(raw=raw, state_dir=state)
    settings.repos = {n: Repo.from_config(n, r, str(raw["coder"])) for n, r in raw["repos"].items()}
    return settings


def make_pool(clock: Clock, backends: list[Backend] | None = None, processes: set[str] | None = None, down: set[str] | None = None) -> Pool:
    running = processes if processes is not None else set()
    offline = down if down is not None else set()

    def get(url: str, **_: Any) -> Resp:
        for name in offline:
            if f"//{name}" in url:
                raise __import__("httpx").ConnectError("down")
        return Resp(200)

    members = backends if backends is not None else [Backend("gpu-a", "http://gpu-a", "ollama", "qwen-coder")]
    return Pool(members, get=get, post=lambda *a, **k: Resp(200), match=lambda wanted: [w for w in wanted if w.lower() in running], clock=clock)


def make_pilot(
    tmp_path: Path, planner_model: FakeModel | None = None, game: FakeGame | None = None, review: FakeModel | None = None,
    pool: Pool | None = None, **over: Any,
) -> tuple[Autopilot, FakeProc, FakeGame, Clock, list[tuple[Any, ...]]]:
    clock = Clock()
    settings = make_settings(tmp_path, **over)
    store = Store(settings.db_path, clock=clock)
    proc = FakeProc()
    game = game or FakeGame()
    audits: list[tuple[Any, ...]] = []
    reviewer = review or FakeModel(default=json.dumps({"verdict": "approve", "summary": "looks fine", "comments": [], "tests_to_add": []}))
    pilot = Autopilot(
        settings, store, Planner(planner_model), game, proc, lambda *a, **k: audits.append((a, k)),
        pool=pool or make_pool(clock), environ={"PATH": "/usr/bin", "HOME": str(tmp_path), "SECRET_THING": "hunter2hunter2", "ANTHROPIC_API_KEY": "sk-test-value-1234"},
        clock=clock, executor=InlineExecutor(), backend_model=lambda b: reviewer, which=lambda _n: None,
    )
    return pilot, proc, game, clock, audits


def run_until(pilot: Autopilot, clock: Clock, done: Callable[[], bool], max_ticks: int = 40, step_seconds: float = 400) -> int:
    for n in range(max_ticks):
        if done():
            return n
        pilot.tick()
        clock.advance(step_seconds)
    raise AssertionError("did not finish: " + repr([(t.id, t.state, t.note) for t in pilot.store.tasks()]))
