"""A pool of local model backends with roles, health checks, leases and failover.

``[autopilot.pool.<name>]`` declares one backend: an almanac gateway on
another machine, an Ollama server, or any OpenAI-compatible server. Each has
``roles`` (planner, coder, reviewer; reviewers also propose missing tests), a number of concurrent
``slots``, and optional availability rules:

* ``unavailable_while_process``: process names (matched like ``[residency]``:
  argv[0] basename in this machine's /proc, Wine paths included) that make the
  backend off-limits while they run, e.g. a game that
  needs that GPU. A job already running on it is stopped within seconds and,
  for an Ollama backend, the model is asked to unload.
* ``check_command``: argv that must exit 0 for the backend to be used (for
  rules about another machine).

Long jobs (local coding sessions, reviews) take a *lease* on one slot, so
several local agents work at once on different GPUs. Short calls (planning,
summaries) use any healthy backend with the role and fail over to the next on
error. If no pool is configured, the ``[gateway]`` backend is the only member.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from ..guard import Guard
from ..residency import running_matches
from .planner import ModelUnavailable

ROLES = ("planner", "coder", "reviewer")
HEALTH_PATHS = {"almanac": "/healthz", "ollama": "/api/version", "openai": "/v1/models"}


@dataclass
class Backend:
    name: str
    url: str
    kind: str = "ollama"  # almanac | ollama | openai
    model: str = "qwen3.5:9b"
    coder_model: str = ""  # model for local coding sessions (default: model)
    roles: list[str] = field(default_factory=lambda: list(ROLES))
    slots: int = 1
    priority: int = 0  # higher first
    enabled: bool = True
    token_file: str = ""
    token_env: str = ""
    unavailable_while_process: list[str] = field(default_factory=list)
    check_command: list[str] = field(default_factory=list)
    local_guard: bool = False  # apply almanac's [guard] (only for a backend on this machine)
    timeout: float = 600.0

    @classmethod
    def from_config(cls, name: str, raw: dict[str, Any]) -> "Backend":
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__ and k != "name"}
        return cls(name=name, **known)

    def token(self) -> str:
        if self.token_env:
            return os.environ.get(self.token_env, "")
        if self.token_file:
            path = Path(os.path.expandvars(self.token_file)).expanduser()
            return path.read_text().strip() if path.is_file() else ""
        return ""

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/")

    @property
    def openai_base(self) -> str:
        return f"{self.base_url}/v1"

    @property
    def label(self) -> str:
        return f"{self.coder_model or self.model}@{self.name}"


@dataclass
class Lease:
    backend: Backend
    role: str

    @property
    def label(self) -> str:
        return self.backend.label


class Pool:
    def __init__(
        self,
        backends: list[Backend],
        get: Callable[..., Any] | None = None,
        post: Callable[..., Any] | None = None,
        match: Callable[[list[str]], list[str]] = running_matches,
        run_check: Callable[[list[str]], int] | None = None,
        clock: Callable[[], float] = time.time,
        ttl: float = 30.0,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.backends = {b.name: b for b in backends}
        self._get = get or httpx.get
        self._post = post or httpx.post
        self._match = match
        self._run_check = run_check or (lambda argv: subprocess.run(argv, capture_output=True, timeout=20).returncode)
        self.clock = clock
        self.ttl = ttl
        self.on_event = on_event or (lambda _m: None)
        self.lock = threading.RLock()
        self.busy: dict[str, int] = {name: 0 for name in self.backends}
        self._health: dict[str, tuple[float, bool, str]] = {}
        self.last_used: dict[str, float] = {}

    @classmethod
    def from_settings(cls, pool_cfg: dict[str, Any], gateway: dict[str, Any], **kwargs: Any) -> "Pool":
        backends = [Backend.from_config(name, raw) for name, raw in pool_cfg.items()]
        if not backends:
            backends = [Backend("default", str(gateway["backend"]), "ollama", str(gateway["default_model"]), local_guard=True)]
        return cls(backends, **kwargs)

    # -- availability ----------------------------------------------------------
    def rule_block(self, backend: Backend) -> str:
        """Why the backend may not be used right now by rule (not health), or ''."""
        if not backend.enabled:
            return "disabled in config"
        if backend.unavailable_while_process:
            hit = self._match(list(backend.unavailable_while_process))
            if hit:
                return f"off-limits while {', '.join(hit)} runs"
        if backend.check_command:
            try:
                if self._run_check(list(backend.check_command)) != 0:
                    return "check_command says no"
            except (OSError, subprocess.SubprocessError) as exc:
                return f"check_command failed ({exc.__class__.__name__})"
        return ""

    def health(self, backend: Backend, force: bool = False) -> tuple[bool, str]:
        with self.lock:
            cached = self._health.get(backend.name)
            if cached and not force and self.clock() - cached[0] < self.ttl:
                return cached[1], cached[2]
        blocked = self.rule_block(backend)
        if blocked:
            ok, reason = False, blocked
        else:
            headers = {"Authorization": f"Bearer {backend.token()}"} if backend.token() else {}
            try:
                response = self._get(backend.base_url + HEALTH_PATHS.get(backend.kind, "/v1/models"), headers=headers, timeout=5)
                ok = int(getattr(response, "status_code", 500)) < 400
                reason = "healthy" if ok else f"HTTP {response.status_code}"
            except (httpx.HTTPError, OSError) as exc:
                ok, reason = False, f"unreachable ({exc.__class__.__name__})"
        with self.lock:
            previous = self._health.get(backend.name)
            self._health[backend.name] = (self.clock(), ok, reason)
        if previous is None or previous[1] != ok:
            self.on_event(f"backend {backend.name}: {'up' if ok else 'down'} ({reason})")
            if not ok and blocked and previous and previous[1]:
                self.unload(backend)
        return ok, reason

    def unload(self, backend: Backend) -> None:
        """Best effort: ask an Ollama backend to drop its model (frees the GPU for the game)."""
        if backend.kind != "ollama":
            return
        for model in {backend.model, backend.coder_model} - {""}:
            try:
                self._post(f"{backend.base_url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=15)
            except httpx.HTTPError:
                pass

    def healthy(self, role: str) -> list[Backend]:
        found = [b for b in self.backends.values() if role in b.roles and self.health(b)[0]]
        return sorted(found, key=lambda b: (-b.priority, b.name))

    # -- leases ----------------------------------------------------------------
    def acquire(self, role: str, avoid: set[str] | None = None) -> Lease | None:
        """A free slot on the best healthy backend with this role, preferring ones not in ``avoid``."""
        candidates = self.healthy(role)
        ordered = [b for b in candidates if b.name not in (avoid or set())] + [b for b in candidates if b.name in (avoid or set())]
        with self.lock:
            for backend in ordered:
                if self.busy[backend.name] < backend.slots:
                    self.busy[backend.name] += 1
                    self.last_used[backend.name] = self.clock()
                    return Lease(backend, role)
        return None

    def release(self, lease: Lease | None) -> None:
        if lease is None:
            return
        with self.lock:
            self.busy[lease.backend.name] = max(0, self.busy[lease.backend.name] - 1)

    def abort_reason(self, lease: Lease) -> Callable[[], str]:
        """For a running job: returns a reason once its backend becomes off-limits (e.g. the game started)."""
        def check() -> str:
            blocked = self.rule_block(lease.backend)
            if blocked:
                self.unload(lease.backend)
            return blocked

        return check

    def status(self) -> list[dict[str, Any]]:
        out = []
        for b in sorted(self.backends.values(), key=lambda b: (-b.priority, b.name)):
            ok, reason = self.health(b)
            out.append({"name": b.name, "kind": b.kind, "model": b.model, "roles": b.roles, "healthy": ok, "reason": reason,
                        "busy": self.busy[b.name], "slots": b.slots, "last_used": self.last_used.get(b.name, 0.0)})
        return out


class BackendModel:
    """Chat completions against one backend (the ``Model`` protocol of the planner)."""

    def __init__(self, backend: Backend, guard_settings: dict[str, Any] | None = None, post: Callable[..., Any] | None = None) -> None:
        self.backend = backend
        self.guard = Guard(guard_settings) if (backend.local_guard and guard_settings is not None) else None
        self._post = post or httpx.post

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        b = self.backend
        if self.guard is not None and b.kind == "ollama":
            try:
                loaded = [m["name"] for m in httpx.get(f"{b.base_url}/api/ps", timeout=5).json().get("models", [])]
            except (httpx.HTTPError, ValueError):
                loaded = []
            verdict = self.guard.may_load(b.model in loaded)
            if not verdict.ok:
                raise ModelUnavailable(verdict.reason)
        payload: dict[str, Any] = {
            "model": b.model, "temperature": 0.1, "reasoning_effort": "none", "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {b.token()}"} if b.token() else {}
        try:
            response = self._post(f"{b.openai_base}/chat/completions", json=payload, headers=headers, timeout=b.timeout)
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"].get("content") or "")
        except (httpx.HTTPError, KeyError, ValueError, IndexError) as exc:
            raise ModelUnavailable(f"{b.name}: request failed ({exc.__class__.__name__})") from exc


class PoolModel:
    """Short calls for one role: first healthy backend, failing over to the next."""

    def __init__(self, pool: Pool, role: str, guard_settings: dict[str, Any] | None = None, factory: Callable[[Backend], Any] | None = None) -> None:
        self.pool = pool
        self.role = role
        self.factory = factory or (lambda b: BackendModel(b, guard_settings))
        self.last_backend = ""

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        errors = []
        for backend in self.pool.healthy(self.role):
            try:
                text = self.factory(backend).complete(system, user, json_mode)
                self.last_backend = backend.name
                self.pool.last_used[backend.name] = self.pool.clock()
                return text
            except ModelUnavailable as exc:
                errors.append(str(exc))
                self.pool.health(backend, force=True)
        raise ModelUnavailable("; ".join(errors) or f"no healthy backend with role {self.role}")
