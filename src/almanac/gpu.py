"""The inference GPU as the model server's machine sees it: name, total and free VRAM.

The Dalamud setup asks the gateway for this (``GET /v1/almanac/gpu``) instead of
measuring from inside the game, which is a poor place for it: under Wine it may
see no GPU at all, or only the one DXVK lets the game render on, and the model
server can be another card or another machine entirely. The gateway runs next to
the model server, so it reads the card models actually load on.

Sources, the first that answers wins: nvidia-smi (NVIDIA), then the amdgpu sysfs
counters (AMD). Neither answers -> an empty list, which callers treat as unknown.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MB = 1024 * 1024


@dataclass(frozen=True)
class Gpu:
    name: str
    vendor: str
    uuid: str
    total_mb: int
    used_mb: int
    free_mb: int
    source: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def nvidia_gpus(run: Callable[..., Any] = subprocess.run) -> list[Gpu]:
    try:
        out = run(
            ["nvidia-smi", "--query-gpu=uuid,name,memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in str(out).splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            total, used, free = (int(p) for p in parts[2:])
        except ValueError:  # "[N/A]" on cards that do not report memory
            continue
        gpus.append(Gpu(parts[1], "nvidia", parts[0], total, used, free, "nvidia-smi"))
    return gpus


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def amd_gpus(drm: Path = Path("/sys/class/drm")) -> list[Gpu]:
    gpus = []
    for card in sorted(drm.glob("card[0-9]*")):
        device = card / "device"
        if "-" in card.name:  # card0-DP-1 and friends are connectors, not cards
            continue
        try:
            total = int(_read(device / "mem_info_vram_total")) // MB
            used = int(_read(device / "mem_info_vram_used")) // MB
        except ValueError:
            continue
        name = _read(device / "product_name") or f"AMD GPU ({card.name})"
        gpus.append(Gpu(name, "amd", _read(device / "unique_id") or card.name, total, used, max(0, total - used), "sysfs"))
    return gpus


def read_gpus() -> list[Gpu]:
    return nvidia_gpus() or amd_gpus()


def inference_gpu(gpus: list[Gpu], uuid: str = "") -> Gpu | None:
    """The card models load on: [guard] gpu_uuid when set, else the one with the most VRAM."""
    if uuid:
        return next((g for g in gpus if g.uuid == uuid), None)
    return max(gpus, key=lambda g: g.total_mb, default=None)
