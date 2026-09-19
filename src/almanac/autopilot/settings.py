"""The ``[autopilot]`` config section, merged over conservative defaults.

Defaults are deliberately tight: dry-run on, two coding runs a day, a small
token and cost cap, no repositories. Nothing happens until the owner lists
repositories under ``[autopilot.repos.<name>]`` and turns dry-run off.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config

DEFAULTS: dict[str, Any] = {
    "dry_run": True,
    "tick_seconds": 20,  # pause between steps while there is work
    "idle_seconds": 120,  # pause when nothing is runnable
    "source_interval_seconds": 900,  # how often sources are polled
    "ticket_poll_seconds": 60,  # how often parked tickets are checked
    "ci_poll_seconds": 300,
    "max_attempts": 3,  # failed attempts before a task goes to the owner
    # Owner approval. Nothing leaves a worktree without an answer here; see approvals.py.
    "approval": {
        "require": ["code", "push", "game_action"],
        "code_scope": "task",  # task: one yes covers the task's coding steps | step: every session
        "allow_session_minutes": 5,  # `almanac autopilot allow` default, sudo-like
        "max_allow_session_minutes": 60,
    },
    # Paths autopilot must never work in, whatever a repo entry says. A repo whose
    # path is inside one of these is dropped from the allow-list at load time.
    "deny_paths": [
        "~/.config", "~/.ssh", "~/.gnupg", "~/.local/share/keyrings", "~/.password-store",
        "~/.claude", "~/.codex", "~/.local/state/almanac", "~/almanac-knowledge", "~/.xlcore",
    ],
    "backoff_base_seconds": 300,
    "backoff_max_seconds": 6 * 3600,
    "kill_switch": "",  # default <state_dir>/autopilot/STOP
    "digest_dir": "",  # default <state_dir>/autopilot/digests
    "digest_hour": 7,  # local hour after which the morning digest is written
    "digest_toast": True,
    "coder": "claude",  # default coder: claude | codex
    "planner_model": "",  # default: [agent] model or [gateway] default_model
    "protected_branches": ["main", "master", "trunk", "release", "production"],
    # Environment for coding sessions. Values are "env:NAME" (copied from
    # autopilot's own environment) or "op://vault/item/field" (1Password,
    # resolved with `op read` at session start). Never literal secrets.
    "secrets": {},
    # Environment variables passed through to sessions and test runs.
    "pass_env": ["PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "XDG_RUNTIME_DIR", "DOTNET_ROOT"],
    "caps": {
        "coding_runs_per_day": 2,
        "tokens_per_day": 200_000,
        "cost_usd_per_day": 2.0,
        "max_turns": 20,
        "max_wall_minutes": 20,
        "test_timeout_minutes": 20,
        "local_steps_per_task": 12,
        # Local coding (no cloud cost): its own caps.
        "local_runs_per_day": 40,
        "local_max_wall_minutes": 30,
        "local_split_parts": 3,  # local coders get smaller scopes: a code step is split into up to N
        "max_files_per_task": 40,  # a branch touching more files than this goes to the owner instead of a PR
        "min_free_memory_mb": 0,  # 0 = off; otherwise coding/test steps wait while free memory is below this
        # Concurrency: steps in flight at once (each on its own task), cloud sessions at once.
        "max_parallel": 3,
        "cloud_slots": 1,
        "review_rounds": 1,  # cross-review -> fix rounds per PR
    },
    "claude": {"argv": ["claude"], "model": "", "extra_args": []},
    "codex": {"argv": ["codex"], "model": "", "extra_args": [], "usd_per_mtok": 0.0},
    # Local coding agent used when cloud coding is capped, rate-limited, out of
    # quota or unauthenticated: "aider" or "codex" (Codex CLI with a local
    # provider), or a custom argv with {prompt} {model} {base_url} {worktree} {test}.
    "local_coder": {"tool": "aider", "argv": [], "extra_args": []},
    # Model backends: [autopilot.pool.<name>] url, kind, model, roles, slots, ...
    # (see pool.py). Empty = the [gateway] backend only.
    "pool": {},
    "sandbox": {
        "mode": "auto",  # auto (bwrap when installed) | bwrap | none
        # Paths the coding CLI itself needs to write besides the worktree.
        "writable": ["~/.claude", "~/.claude.json", "~/.codex", "~/.cache"],
        # Hidden from sessions and tests (credentials of other tools).
        "hide": ["~/.config/almanac", "~/.config/gh", "~/.config/op", "~/.xlcore/pluginConfigs", "~/.aws", "~/.docker"],
    },
    "sources": {
        "github": {"enabled": False, "label": "autopilot", "limit": 20},
        "inbox": {"enabled": False, "path": ""},
        "vote": {"enabled": False, "url": "", "top": 3, "repo": ""},
        "ci": {"enabled": False, "limit": 5},
    },
    "repos": {},
    "game": {
        "enabled": True,
        "upstream": "xivmcp",
        "board_agent": "autopilot",
        # Candidate names for a quest-tracker objective tool; the first one the
        # server lists is used. Absent on the server = objectives are skipped.
        "objective_tools": ["set_tracker_objectives", "set_quest_tracker_objectives", "set_objectives"],
        "toast_tool": "show_toast",
        # Tools the planner may use in game_read steps (tier read/ui only).
        "read_tools": [],
    },
}

REPO_DEFAULTS: dict[str, Any] = {
    "path": "",
    "github": "",  # owner/name, used for gh --repo
    "remote": "origin",  # "" for a repo without a remote: branches stay local, no PR
    "base": "main",
    "test": [],  # argv run inside the worktree, e.g. ["tests/run.sh"]
    "setup": [],  # optional argv before tests (e.g. dependency restore)
    "coder": "",  # default: [autopilot] coder
    "allow_ssh_hosts": [],  # hosts a coding session may ssh to (default: none)
    "allow_network": True,  # coding CLIs need their API; False only for fully local coders
    "priority": 0,  # added to every task's value for this repo
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key not in ("repos", "secrets", "pool"):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def expand(path: str) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser()


def append_log(path: Path, task_id: int | None, kind: str, message: str) -> None:
    """Append one timestamped line to the durable action log.

    Best effort on purpose: a full disk or a read-only home must not stop the
    loop, and the same events are in the queue database either way.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    where = f"#{task_id}" if task_id else "-"
    line = f"{stamp} {where:>6} {kind:10} {' | '.join(message.splitlines())[:1500]}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


@dataclass
class Repo:
    name: str
    path: Path
    github: str
    remote: str
    base: str
    test: list[str]
    setup: list[str]
    coder: str
    allow_ssh_hosts: list[str]
    allow_network: bool
    priority: int

    @property
    def base_ref(self) -> str:
        return f"{self.remote}/{self.base}" if self.remote else self.base

    @classmethod
    def from_config(cls, name: str, raw: dict[str, Any], default_coder: str) -> "Repo":
        data = {**REPO_DEFAULTS, **raw}
        return cls(
            name=name,
            path=expand(data["path"]),
            github=str(data["github"]),
            remote=str(data["remote"]),
            base=str(data["base"]),
            test=[str(a) for a in data["test"]],
            setup=[str(a) for a in data["setup"]],
            coder=str(data["coder"] or default_coder),
            allow_ssh_hosts=[str(h) for h in data["allow_ssh_hosts"]],
            allow_network=bool(data["allow_network"]),
            priority=int(data["priority"]),
        )


@dataclass
class Settings:
    raw: dict[str, Any]
    state_dir: Path
    repos: dict[str, Repo] = field(default_factory=dict)
    denied: dict[str, str] = field(default_factory=dict)  # repo name -> why it was dropped

    @classmethod
    def from_config(cls, config: Config) -> "Settings":
        raw = _merge(DEFAULTS, config.section("autopilot"))
        base = config.state_dir / "autopilot"
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings = cls(raw=raw, state_dir=base)
        for name, spec in dict(raw.get("repos", {})).items():
            repo = Repo.from_config(name, spec, str(raw["coder"]))
            forbidden = settings.forbids(repo.path)
            if forbidden:
                settings.denied[name] = forbidden
            else:
                settings.repos[name] = repo
        return settings

    def forbids(self, path: Path) -> str:
        """The deny_paths entry containing ``path``, or "" when it is allowed."""
        try:
            target = path.expanduser().resolve()
        except OSError:  # pragma: no cover - unreadable path
            target = path
        for raw in self.raw.get("deny_paths", []):
            try:
                denied = expand(str(raw)).resolve()
            except OSError:  # pragma: no cover
                continue
            if target == denied or denied in target.parents:
                return str(raw)
        return ""

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def section(self, key: str) -> dict[str, Any]:
        return dict(self.raw.get(key, {}))

    @property
    def caps(self) -> dict[str, Any]:
        return self.section("caps")

    @property
    def db_path(self) -> Path:
        return self.state_dir / "queue.sqlite"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    @property
    def kill_switch(self) -> Path:
        return expand(self.raw["kill_switch"]) if self.raw["kill_switch"] else self.state_dir / "STOP"

    @property
    def digest_dir(self) -> Path:
        return expand(self.raw["digest_dir"]) if self.raw["digest_dir"] else self.state_dir / "digests"

    @property
    def log_file(self) -> Path:
        """Plain-text, append-only action log: what autopilot did, with timestamps."""
        return self.state_dir / "autopilot.log"

    @property
    def approval(self) -> dict[str, Any]:
        return self.section("approval")

    @property
    def protected(self) -> set[str]:
        return {str(b) for b in self.raw["protected_branches"]}
