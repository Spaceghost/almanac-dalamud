"""Running child processes: minimal environment, secrets from references, optional bubblewrap.

* Environment: only ``pass_env`` names plus the resolved ``secrets``; nothing
  else from autopilot's own environment leaks into a coding session.
* Secrets: each value is a *reference*, ``env:NAME`` or ``op://vault/item/field``
  (1Password, resolved with ``op read`` when a session starts). Literal values
  are refused. Resolved values are registered with the ``Redactor`` so they
  are masked in every log line, task log and digest.
* Sandbox (bwrap): the filesystem is read-only except the task's worktree, the
  repository's git directory (commits made in a worktree land there), and the
  coding CLI's own state dirs. ``~/.ssh`` and almanac's config (its token) are
  hidden unless the repository allows ssh. ``no_new_privs`` is set, so sudo
  cannot elevate. Without bwrap the same environment rules apply, and the
  coding CLI's own permission rules are the only filesystem fence.
* Every run is time-limited and checks the kill switch file once a second, and
  an optional abort check (e.g. "the game started, this GPU is off-limits")
  every five seconds.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .settings import expand


class SecretError(RuntimeError):
    pass


class Redactor:
    def __init__(self) -> None:
        self.values: set[str] = set()

    def add(self, value: str) -> None:
        if value and len(value) >= 6:
            self.values.add(value)

    def __call__(self, text: str) -> str:
        for value in sorted(self.values, key=len, reverse=True):
            text = text.replace(value, "***")
        return text


def resolve_secrets(
    refs: dict[str, str], environ: dict[str, str], redactor: Redactor, op_read: Callable[[str], str] | None = None
) -> dict[str, str]:
    """Turn {NAME: "env:X" | "op://..."} into {NAME: value}. Missing values are skipped, literals refused."""
    out: dict[str, str] = {}
    for name, ref in refs.items():
        ref = str(ref)
        if ref.startswith("env:"):
            value = environ.get(ref[4:], "")
        elif ref.startswith("op://"):
            value = (op_read or _op_read)(ref)
        else:
            raise SecretError(f"secret {name}: only env:NAME or op:// references are allowed, not literal values")
        if value:
            redactor.add(value)
            out[name] = value
    return out


def _op_read(ref: str) -> str:
    try:
        proc = subprocess.run(["op", "read", "--no-newline", ref], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretError(f"1Password CLI failed for a reference ({exc.__class__.__name__})") from exc
    if proc.returncode != 0:
        raise SecretError("1Password CLI could not read a reference (is `op` signed in / a service account token set?)")
    return proc.stdout


@dataclass
class ProcResult:
    code: int
    output: str
    seconds: float
    stopped: str = ""  # "timeout" | "kill switch" | ""

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.stopped


class ProcRunner(Protocol):
    def __call__(
        self, argv: list[str], cwd: Path, env: dict[str, str], timeout: float, stdin: str | None = None,
        abort: Callable[[], str] | None = None,
    ) -> ProcResult: ...


class SubprocessRunner:
    """Real runner: own process group, wall-clock limit, kill switch, output capped."""

    def __init__(self, kill_switch: Path, max_output: int = 400_000) -> None:
        self.kill_switch = kill_switch
        self.max_output = max_output

    def __call__(
        self, argv: list[str], cwd: Path, env: dict[str, str], timeout: float, stdin: str | None = None,
        abort: Callable[[], str] | None = None,
    ) -> ProcResult:
        import threading

        start = time.monotonic()
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True,
        )
        chunks: list[str] = []
        assert proc.stdout is not None
        reader = threading.Thread(target=lambda: chunks.extend(iter(proc.stdout.readline, "")), daemon=True)  # type: ignore[union-attr]
        reader.start()
        if stdin is not None and proc.stdin:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        stopped = ""
        last_abort_check = 0.0
        while proc.poll() is None:
            if time.monotonic() - start > timeout:
                stopped = "timeout"
            elif self.kill_switch.exists():
                stopped = "kill switch"
            elif abort is not None and time.monotonic() - last_abort_check >= 5:
                last_abort_check = time.monotonic()
                reason = abort()
                if reason:
                    stopped = f"aborted: {reason}"
            if stopped:
                self._kill(proc)
                break
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        reader.join(timeout=5)
        output = "".join(chunks)
        if len(output) > self.max_output:
            output = output[: self.max_output // 4] + "\n...(truncated)...\n" + output[-self.max_output // 2 :]
        return ProcResult(proc.returncode if proc.returncode is not None else -9, output, round(time.monotonic() - start, 1), stopped)

    @staticmethod
    def _kill(proc: subprocess.Popen[str]) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                continue


def child_env(pass_env: list[str], environ: dict[str, str], secrets: dict[str, str], allow_ssh: bool) -> dict[str, str]:
    env = {name: environ[name] for name in pass_env if name in environ}
    if allow_ssh and "SSH_AUTH_SOCK" in environ:
        env["SSH_AUTH_SOCK"] = environ["SSH_AUTH_SOCK"]
    env.update(secrets)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["ALMANAC_AUTOPILOT"] = "1"
    return env


def sandbox_mode(configured: str, which: Callable[[str], str | None] = shutil.which) -> str:
    if configured == "auto":
        return "bwrap" if which("bwrap") else "none"
    return configured


def bwrap_argv(
    worktree: Path, git_dir: Path | None, writable: list[str], allow_ssh: bool, allow_network: bool, hide: list[Path]
) -> list[str]:
    argv = [
        "bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
    ]
    if not allow_network:
        argv.append("--unshare-net")
    binds = [worktree] + ([git_dir] if git_dir else []) + [expand(p) for p in writable]
    for path in binds:
        if path.exists():
            argv += ["--bind", str(path), str(path)]
    hidden = list(hide) + ([] if allow_ssh else [expand("~/.ssh")])
    for path in hidden:
        if path.is_dir():
            argv += ["--tmpfs", str(path)]
        elif path.exists():
            argv += ["--ro-bind", "/dev/null", str(path)]
    argv += ["--chdir", str(worktree), "--"]
    return argv


def redact_env_for_log(env: dict[str, Any], secret_names: set[str]) -> dict[str, str]:
    return {k: ("***" if k in secret_names else str(v)) for k, v in env.items()}
