"""Configuration: one TOML file layered over built-in defaults.

Lookup order: $ALMANAC_CONFIG, then ~/.config/almanac/config.toml. Every key
has a default, so an empty or missing file is valid. See config.example.toml.

Knowledge and tools are *not* part of the engine: ``knowledge_dirs`` and
``tools_dirs`` list directories (usually a private git repo of your own).
The defaults point at the bundled examples so a fresh checkout works.
$ALMANAC_KNOWLEDGE / $ALMANAC_TOOLS (colon-separated) override both.
"""

from __future__ import annotations

import os
import secrets
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULTS: dict[str, Any] = {
    "knowledge_dirs": [str(REPO_ROOT / "examples" / "knowledge")],
    "tools_dirs": [str(REPO_ROOT / "examples" / "tools")],
    "disabled_tools": [],
    "state_dir": "~/.local/state/almanac",
    "token_file": "~/.config/almanac/token",
    "this_host": "",
    # name -> {transport = "local"} or {transport = "ssh", ssh = "<ssh destination>"}
    "hosts": {"local": {"transport": "local"}},
    "mcp": {"listen": ["127.0.0.1:41880"]},
    "gateway": {
        "listen": ["127.0.0.1:41881"],
        "backend": "http://127.0.0.1:11434",
        "default_model": "qwen3.5:9b",
        # Only these backend models may be loaded through almanac. Keep it to
        # models that fit entirely in the inference GPU's VRAM.
        "allowed_models": ["qwen3.5:9b"],
        # Client model name (fnmatch pattern) -> backend model. First match wins.
        "models": {"claude-*": "qwen3.5:9b", "gpt-*": "qwen3.5:9b", "local*": "qwen3.5:9b"},
        "request_timeout": 600,
    },
    "guard": {
        # Refuse to load a model when host MemAvailable is below this.
        "min_mem_available_mb": 1500,
        # Unload the loaded model when MemAvailable drops below this.
        "unload_below_mem_available_mb": 900,
        # Refuse to load when the inference GPU has less free VRAM than this.
        "min_gpu_free_mb": 6500,
        # nvidia-smi -i value (UUID) of the inference GPU; empty = skip VRAM check.
        "gpu_uuid": "",
        "poll_seconds": 15,
    },
    "residency": {
        # Keep the model loaded and pinned while any of these processes runs
        # (basename of argv[0], case-insensitive; Wine's C:\...\x.exe paths match).
        "keep_loaded_while_process": [],
        # keep_alive restored when they exit, so the model unloads later as usual.
        "idle_keep_alive": "5m",
        "poll_seconds": 20,
        # Load the model as soon as a process appears (false: only pin once a request loaded it).
        "preload": True,
        "model": "",  # empty = [gateway] default_model
    },
    "kb": {"embed_model": ""},
    "agent": {"max_steps": 8, "model": "", "temperature": 0.2, "reasoning_effort": "none",
              # the model's context window in tokens: thread history is trimmed to fit
              "context_tokens": 8192},
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key not in ("hosts", "models"):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        candidate = Path(path or os.environ.get("ALMANAC_CONFIG") or "~/.config/almanac/config.toml").expanduser()
        data: dict[str, Any] = {}
        if candidate.is_file():
            data = tomllib.loads(candidate.read_text())
        return cls(_merge(DEFAULTS, data))

    def _path(self, key: str) -> Path:
        return Path(os.path.expandvars(str(self.raw[key]))).expanduser()

    def _paths(self, key: str, env: str) -> list[Path]:
        override = os.environ.get(env)
        items = override.split(":") if override else list(self.raw[key])
        return [Path(os.path.expandvars(str(item))).expanduser() for item in items if item]

    @property
    def knowledge_dirs(self) -> list[Path]:
        """Knowledge roots, highest priority first. New notes go to the first."""
        return self._paths("knowledge_dirs", "ALMANAC_KNOWLEDGE")

    @property
    def tools_dirs(self) -> list[Path]:
        """Tool directories; a later directory's tool replaces an earlier one's of the same name."""
        return self._paths("tools_dirs", "ALMANAC_TOOLS")

    @property
    def state_dir(self) -> Path:
        path = self._path("state_dir")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    @property
    def token_file(self) -> Path:
        return self._path("token_file")

    @property
    def this_host(self) -> str:
        return str(self.raw.get("this_host") or socket.gethostname().split(".")[0])

    @property
    def hosts(self) -> dict[str, dict[str, Any]]:
        return dict(self.raw["hosts"])

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get(name, {}))

    def read_token(self) -> str:
        """Return the shared bearer token. Never log or print the result."""
        return self.token_file.read_text().strip()

    def ensure_token(self) -> bool:
        """Create the token file (0600) if missing. Returns True if created."""
        path = self.token_file
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
        return True
