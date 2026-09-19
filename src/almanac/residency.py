"""Model residency: keep the model loaded while chosen processes run.

Configured by ``[residency]``. While any process named in
``keep_loaded_while_process`` is running, the gateway makes sure the model is
loaded and pinned (Ollama ``keep_alive: -1``); when they are all gone it sets
``keep_alive`` back to ``idle_keep_alive`` so the model unloads later as it
would have anyway. It runs as a loop inside the gateway, not as a daemon.

* Processes are matched on the basename of ``argv[0]`` from
  ``/proc/<pid>/cmdline`` (case-insensitive). Windows paths (``Z:\\...\\x.exe``,
  as Wine shows them) are split on ``\\`` as well as ``/``, and when argv[0] is
  a Wine loader its first argument is checked too.
* Pinning respects the resource guard: if the model is not resident and the
  guard refuses to load it (memory, GPU VRAM), the loop logs once and retries
  on the next poll instead of failing.
* The pin is re-asserted whenever the model is missing (backend restarted,
  memory-guard unload) or no longer pinned (a request reset its keep_alive).
* State changes are logged once; the current state is kept in memory for
  ``/healthz`` and written to ``<state_dir>/residency.json`` for the MCP tool
  ``model_residency``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .guard import Guard

log = logging.getLogger("almanac.residency")

WINE_LOADERS = {"wine", "wine64", "wine-preloader", "wine64-preloader", "wineloader", "wine.exe"}
STATE_FILE = "residency.json"
DEFAULTS: dict[str, Any] = {
    "keep_loaded_while_process": [],
    "idle_keep_alive": "5m",
    "poll_seconds": 20,
    "preload": True,
    "model": "",  # empty = [gateway] default_model
}


def basename(arg: str) -> str:
    """Last path component, for POSIX and Windows paths alike, lower-cased."""
    return arg.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


def process_names(argv: list[str]) -> list[str]:
    """Names a process can be matched by: argv[0], and argv[1] under a Wine loader."""
    if not argv or not argv[0]:
        return []
    names = [basename(argv[0])]
    if names[0] in WINE_LOADERS and len(argv) > 1:
        names.append(basename(argv[1]))
    return names


def running_matches(wanted: list[str], proc_root: Path = Path("/proc")) -> list[str]:
    """The wanted names (as configured) that have at least one running process."""
    targets = {w.lower(): w for w in wanted}
    found: set[str] = set()
    if not targets:
        return []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:  # exited, or not ours to read
            continue
        argv = [a.decode(errors="replace") for a in raw.split(b"\0")]
        for name in process_names(argv):
            if name in targets:
                found.add(targets[name])
    return sorted(found)


def is_pinned(expires_at: str, now: float | None = None) -> bool:
    """Ollama reports keep_alive -1 as an expiry centuries away (e.g. year 2318)."""
    try:
        year = int(str(expires_at)[:4])
    except ValueError:
        return False
    return year > time.gmtime(now).tm_year + 50


class Residency:
    """The residency state machine. ``tick()`` is one poll; ``run()`` loops it.

    States: ``disabled`` (no processes configured), ``idle`` (none running,
    nothing pinned by us), ``pinned``, ``deferred`` (wanted but the guard
    refused to load), ``backend_down`` (wanted but the backend did not answer).
    """

    def __init__(
        self,
        settings: dict[str, Any],
        model: str,
        backend: str,
        client: httpx.AsyncClient,
        guard: Guard,
        state_dir: Path | None = None,
        processes: Callable[[list[str]], list[str]] = running_matches,
        clock: Callable[[], float] = time.time,
    ) -> None:
        merged = {**DEFAULTS, **settings}
        self.wanted = [str(p) for p in merged["keep_loaded_while_process"]]
        self.idle_keep_alive = merged["idle_keep_alive"]
        self.poll = float(merged["poll_seconds"])
        self.preload = bool(merged["preload"])
        self.model = str(merged["model"] or model)
        self.backend = backend.rstrip("/")
        self.client = client
        self.guard = guard
        self.state_file = state_dir / STATE_FILE if state_dir else None
        self._processes = processes
        self._clock = clock
        self.state = "disabled" if not self.wanted else "idle"
        self.reason = "" if self.wanted else "no processes configured"
        self.running: list[str] = []
        self.since = clock()
        self.checked = 0.0
        self._pinned_by_us = False

    @property
    def enabled(self) -> bool:
        return bool(self.wanted)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "reason": self.reason,
            "model": self.model,
            "watching": self.wanted,
            "running": self.running,
            "preload": self.preload,
            "idle_keep_alive": self.idle_keep_alive,
            "poll_seconds": self.poll,
            "since": _iso(self.since),
            "checked": _iso(self.checked) if self.checked else None,
        }

    # -- backend -------------------------------------------------------------
    async def _resident(self) -> dict[str, Any] | None:
        """This model's /api/ps entry, or None if not loaded. Raises if the backend is down."""
        response = await self.client.get(f"{self.backend}/api/ps", timeout=5)
        response.raise_for_status()
        for entry in response.json().get("models", []):
            if self.model in (entry.get("name"), entry.get("model")):
                return entry
        return None

    async def _keep_alive(self, value: Any) -> None:
        # No prompt: Ollama loads the model (if needed) and sets keep_alive without generating.
        response = await self.client.post(
            f"{self.backend}/api/generate", json={"model": self.model, "keep_alive": value}, timeout=300
        )
        response.raise_for_status()

    # -- state machine ---------------------------------------------------------
    def _set(self, state: str, reason: str, level: int = logging.INFO) -> None:
        # Log transitions only; the reason may carry live numbers that change every poll.
        if state != self.state:
            log.log(level, "residency %s: %s", state, reason)
            self.since = self._clock()
        self.state, self.reason = state, reason

    async def tick(self) -> dict[str, Any]:
        if not self.enabled:
            return self.snapshot()
        self.checked = self._clock()
        self.running = await asyncio.to_thread(self._processes, self.wanted)
        try:
            if self.running:
                await self._hold()
            else:
                await self._release()
        except (httpx.HTTPError, ValueError) as exc:
            self._set("backend_down", f"backend did not answer ({exc.__class__.__name__}); retrying every {self.poll:g}s", logging.WARNING)
        self._write()
        return self.snapshot()

    async def _hold(self) -> None:
        who = ", ".join(self.running)
        entry = await self._resident()
        if entry is not None and is_pinned(str(entry.get("expires_at", ""))):
            self._pinned_by_us = True
            self._set("pinned", f"{self.model} pinned while {who} runs")
            return
        if entry is None and not self.preload:
            self._set("idle", f"{who} running; preload is off, pinning once the model is loaded by a request")
            return
        if entry is None:
            verdict = await asyncio.to_thread(self.guard.may_load, False)
            if not verdict.ok:
                self._set("deferred", f"{who} running but {verdict.reason}; retrying every {self.poll:g}s", logging.WARNING)
                return
        await self._keep_alive(-1)
        if self.state == "pinned":  # it was pinned and got unloaded or reset: say so once
            log.info("residency re-pinned %s (%s)", self.model, "was unloaded" if entry is None else "keep_alive had been reset")
        self._pinned_by_us = True
        self._set("pinned", f"{self.model} pinned while {who} runs")

    async def _release(self) -> None:
        if self._pinned_by_us:
            entry = await self._resident()
            if entry is not None:  # a keep_alive request would load it if it were not resident
                await self._keep_alive(self.idle_keep_alive)
            self._pinned_by_us = False
            self._set("idle", f"released: watched processes exited; keep_alive back to {self.idle_keep_alive}")
        elif self.state != "idle":
            self._set("idle", "no watched process running")

    def _write(self) -> None:
        if self.state_file is None:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot(), indent=1) + "\n")
            os.replace(tmp, self.state_file)
        except OSError as exc:
            log.debug("cannot write %s: %s", self.state_file, exc)

    async def run(self) -> None:
        if not self.enabled:
            return
        log.info("residency: keeping %s loaded while %s runs", self.model, ", ".join(self.wanted))
        while True:
            try:
                await self.tick()
            except Exception:  # keep the loop alive whatever happens
                log.exception("residency tick failed")
            await asyncio.sleep(self.poll)


def read_state(state_dir: Path) -> dict[str, Any] | None:
    """The gateway's last published residency state, or None if it never wrote one."""
    try:
        return json.loads((state_dir / STATE_FILE).read_text())
    except (OSError, ValueError):
        return None


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))
