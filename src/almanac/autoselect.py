"""``auto``: the largest configured model that fits the VRAM free right now.

A client (or a ``[gateway.models]`` mapping) asking for ``auto`` gets, per request,
the first model of ``[gateway] auto_models`` (best first) whose footprint fits

    free VRAM + what almanac's own models hold (freed by switching) - headroom

so one config serves a quiet GPU with the big model and a GPU shared with a game
with a smaller one, without anyone editing numbers when the card or the game
changes. Headroom defaults to a tenth of the card (at least 512 MB).

A model's footprint is what Ollama reported (``size_vram``) the last time it was
loaded, kept in the state dir; until one has been seen, its file size plus a
fifth (weights plus KV cache and compute buffers at a moderate context).

Other workloads on the same card (a game) can *reserve* VRAM through the
gateway (``PUT /v1/almanac/gpu/reservations/<owner>``). While reservations are
held, almanac's models are also capped at

    total VRAM - reserved - headroom

so a game that is still loading (and has not allocated yet) is not crowded out
by a model picked in that moment. A reservation may carry a TTL; the holder
refreshes it, so one whose holder died lapses on its own.

When the GPU cannot be read at all (no nvidia-smi, no amdgpu sysfs, a VM without
the card), ``auto_fallback`` is used, else the last (smallest) of ``auto_models``:
something always runs, and Ollama spills to the CPU if it has to.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .gpu import MB, Gpu

log = logging.getLogger("almanac.auto")

AUTO = "auto"
FOOTPRINT_FILE = "auto_footprints.json"
RESERVATION_FILE = "gpu_reservations.json"
ESTIMATE_FACTOR = 1.2  # file size -> loaded size, before a real load has been seen


@dataclass(frozen=True)
class Choice:
    model: str
    reason: str
    budget_mb: int | None = None  # None: the GPU could not be read
    need_mb: int | None = None


class Footprints:
    """Measured VRAM per model (MB), remembered across restarts."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self.file = state_dir / FOOTPRINT_FILE if state_dir else None
        self.mb: dict[str, int] = {}
        if self.file:
            try:
                self.mb = {str(k): int(v) for k, v in json.loads(self.file.read_text()).items()}
            except (OSError, ValueError, AttributeError):
                self.mb = {}

    def record(self, model: str, size_vram_bytes: int) -> None:
        mb = int(size_vram_bytes) // MB
        if mb <= 0 or self.mb.get(model) == mb:
            return
        self.mb[model] = mb
        if self.file:
            try:
                self.file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.file.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.mb, indent=1, sort_keys=True) + "\n")
                os.replace(tmp, self.file)
            except OSError as exc:
                log.debug("cannot write %s: %s", self.file, exc)


class Reservations:
    """VRAM other workloads hold on the card: owner -> MB, each with an optional expiry. Kept across restarts."""

    def __init__(self, state_dir: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.file = state_dir / RESERVATION_FILE if state_dir else None
        self._clock = clock
        self._held: dict[str, tuple[int, float | None]] = {}
        if self.file:
            try:
                raw = json.loads(self.file.read_text())
                self._held = {str(k): (int(v["mb"]), None if v.get("expires") is None else float(v["expires"])) for k, v in raw.items()}
            except (OSError, ValueError, AttributeError, KeyError, TypeError):
                self._held = {}

    def _save(self) -> None:
        if not self.file:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: {"mb": mb, "expires": exp} for k, (mb, exp) in self._held.items()}, indent=1, sort_keys=True) + "\n")
            os.replace(tmp, self.file)
        except OSError as exc:
            log.debug("cannot write %s: %s", self.file, exc)

    def current(self) -> dict[str, int]:
        now = self._clock()
        lapsed = [k for k, (_, exp) in self._held.items() if exp is not None and exp <= now]
        for owner in lapsed:
            log.info("gpu reservation by %s lapsed", owner)
            del self._held[owner]
        if lapsed:
            self._save()
        return {k: mb for k, (mb, _) in self._held.items()}

    def total_mb(self) -> int:
        return sum(self.current().values())

    def hold(self, owner: str, mb: int, ttl_s: float | None = None) -> None:
        if owner not in self._held or self._held[owner][0] != mb:
            log.info("gpu reservation: %s holds %d MB", owner, mb)
        self._held[owner] = (int(mb), None if ttl_s is None else self._clock() + float(ttl_s))
        self._save()

    def release(self, owner: str) -> bool:
        if self._held.pop(owner, None) is None:
            return False
        log.info("gpu reservation by %s released", owner)
        self._save()
        return True


class AutoSelector:
    def __init__(self, settings: dict[str, Any], gpu: Callable[[], Gpu | None], footprints: Footprints,
                 reservations: Reservations | None = None) -> None:
        self.models = [str(m) for m in settings.get("auto_models", [])]
        self.fallback = str(settings.get("auto_fallback") or (self.models[-1] if self.models else ""))
        headroom = settings.get("auto_headroom_mb")
        self.headroom = None if headroom in (None, "") else int(headroom)
        self._gpu = gpu
        self.footprints = footprints
        self.reservations = reservations or Reservations()

    @property
    def enabled(self) -> bool:
        return bool(self.models)

    def headroom_mb(self, total_mb: int) -> int:
        return self.headroom if self.headroom is not None else max(512, total_mb // 10)

    def need_mb(self, model: str, file_bytes: int | None) -> int | None:
        if model in self.footprints.mb:
            return self.footprints.mb[model]
        if file_bytes:
            return math.ceil(file_bytes / MB * ESTIMATE_FACTOR)
        return None

    def budget(self, gpu: Gpu, loaded: dict[str, int]) -> int:
        """VRAM a model may take: free now, plus what our own resident models would give back,
        and never more than the card less what others reserved."""
        headroom = self.headroom_mb(gpu.total_mb)
        reclaim = sum(size for name, size in loaded.items() if name in self.models) // MB
        budget = gpu.free_mb + reclaim - headroom
        reserved = self.reservations.total_mb()
        if reserved:
            budget = min(budget, gpu.total_mb - reserved - headroom)
        return budget

    def choose(self, installed: dict[str, int], loaded: dict[str, int]) -> Choice:
        """``installed``: model -> file size (bytes), from /api/tags. ``loaded``: model -> size_vram, from /api/ps."""
        for name, size in loaded.items():
            if name in self.models:
                self.footprints.record(name, size)
        candidates = [m for m in self.models if m in installed] or self.models
        gpu = self._gpu()
        if gpu is None:
            return Choice(self.fallback, "cannot read the inference GPU's memory; using the fallback model")
        budget = self.budget(gpu, loaded)
        for model in candidates:
            need = self.need_mb(model, installed.get(model))
            if need is not None and need <= budget:
                return Choice(model, f"{model} needs about {need} MB of {budget} MB available", budget, need)
        smallest = candidates[-1]
        need = self.need_mb(smallest, installed.get(smallest))
        return Choice(smallest, f"nothing fits {budget} MB; {smallest} (the smallest) runs partly on the CPU", budget, need)
