"""Resource guard: keep inference off when the machine is busy.

The gateway (and ``almanac ask``) asks ``Guard.may_load`` before a request
that would load a model which is not already resident. A background loop
(``Guard.watch``) unloads the resident model when host memory runs low.
Idle unload itself is Ollama's ``keep_alive`` (set on the Ollama service).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Verdict:
    ok: bool
    reason: str


def mem_available_mb(meminfo: Path = Path("/proc/meminfo")) -> int:
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError("MemAvailable not found in /proc/meminfo")


def gpu_free_mb(uuid: str) -> int | None:
    """Free VRAM of one GPU (by UUID) via nvidia-smi, or None if unavailable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "-i", uuid, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


class Guard:
    def __init__(self, settings: dict[str, Any], mem_reader: Any = mem_available_mb, gpu_reader: Any = gpu_free_mb) -> None:
        self.min_mem = int(settings.get("min_mem_available_mb", 1500))
        self.unload_below = int(settings.get("unload_below_mem_available_mb", 900))
        self.min_gpu = int(settings.get("min_gpu_free_mb", 0))
        self.gpu_uuid = str(settings.get("gpu_uuid", ""))
        self.poll = float(settings.get("poll_seconds", 15))
        self._mem = mem_reader
        self._gpu = gpu_reader

    def may_load(self, already_loaded: bool, picked_by_auto: bool = False) -> Verdict:
        """``picked_by_auto``: ``auto`` chose the model for the VRAM free right now, or as its fallback when the
        GPU cannot be read (autoselect.py), so the fixed ``min_gpu_free_mb`` does not apply; host memory still does."""
        if already_loaded:
            return Verdict(True, "model already resident")
        mem = self._mem()
        if mem < self.min_mem:
            return Verdict(False, f"host memory is tight ({mem} MB available < {self.min_mem} MB); not loading a model")
        if self.gpu_uuid and self.min_gpu and not picked_by_auto:
            free = self._gpu(self.gpu_uuid)
            if free is None:
                return Verdict(False, "cannot read the inference GPU's free memory; not loading a model")
            if free < self.min_gpu:
                return Verdict(False, f"inference GPU has {free} MB free < {self.min_gpu} MB; not loading a model")
        return Verdict(True, f"{mem} MB host memory available")

    def should_unload(self) -> bool:
        return self._mem() < self.unload_below
