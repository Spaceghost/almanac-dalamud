"""Declared tools: one TOML file per tool in tools/, run as argv arrays.

A tool file is data, not code. The runner:

1. validates arguments against the declared parameters (type, enum, pattern,
   min/max; strings may not start with ``-`` unless ``allow_dash = true``),
2. substitutes them into argv *elements* (never through a shell),
3. runs each command locally or on the target host (ssh, argv quoted with
   shlex so the remote POSIX shell sees exactly one word per element),
4. enforces a timeout, captures stdout+stderr and truncates the output.

See tools/README.md for the file format.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import REPO_ROOT

SAFETY = ("read", "change", "destructive")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{(@?[a-z_][a-z0-9_]*)\}")
DEFAULT_STRING_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:/@+=-]{0,127}"
BUILTINS = {
    "@home": str(Path.home()),
    "@repo": str(REPO_ROOT),
    "@python": os.environ.get("ALMANAC_PYTHON", "") or __import__("sys").executable,
}


class ToolError(ValueError):
    pass


@dataclass
class Param:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    default: Any = None
    enum: list[Any] | None = None
    pattern: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    allow_dash: bool = False

    def schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            out["enum"] = self.enum
        if self.type == "string" and not self.enum:
            out["pattern"] = f"^(?:{self.pattern or DEFAULT_STRING_PATTERN})$"
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.default is not None:
            out["default"] = self.default
        return out

    def check(self, value: Any) -> Any:
        if self.type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
                    value = int(value)
                else:
                    raise ToolError(f"{self.name}: expected an integer")
            if self.minimum is not None and value < self.minimum:
                raise ToolError(f"{self.name}: must be >= {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ToolError(f"{self.name}: must be <= {self.maximum}")
        elif self.type == "boolean":
            if isinstance(value, str) and value.lower() in ("true", "false"):
                value = value.lower() == "true"
            if not isinstance(value, bool):
                raise ToolError(f"{self.name}: expected true or false")
        else:
            if not isinstance(value, str):
                raise ToolError(f"{self.name}: expected a string")
            if "\x00" in value or "\n" in value:
                raise ToolError(f"{self.name}: control characters are not allowed")
            if value.startswith("-") and not self.allow_dash:
                raise ToolError(f"{self.name}: may not start with '-'")
            if not self.enum and not re.fullmatch(self.pattern or DEFAULT_STRING_PATTERN, value):
                raise ToolError(f"{self.name}: does not match {self.pattern or DEFAULT_STRING_PATTERN}")
        if self.enum and value not in self.enum:
            raise ToolError(f"{self.name}: must be one of {', '.join(map(str, self.enum))}")
        return value


@dataclass
class Command:
    argv: list[Any]
    hosts: list[str] | None = None
    init: list[str] | None = None
    label: str = ""


@dataclass
class Tool:
    name: str
    description: str
    safety: str
    run_on: str
    hosts: list[str]
    commands: list[Command]
    params: dict[str, Param] = field(default_factory=dict)
    timeout: int = 60
    max_output: int = 12000
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    source: str = ""

    def input_schema(self) -> dict[str, Any]:
        props = {name: p.schema() for name, p in self.params.items()}
        required = [name for name, p in self.params.items() if p.required]
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        unknown = set(args) - set(self.params)
        if unknown:
            raise ToolError(f"unknown argument(s): {', '.join(sorted(unknown))}")
        clean: dict[str, Any] = {}
        for name, param in self.params.items():
            if args.get(name) is None:
                if param.required:
                    raise ToolError(f"{name}: required")
                if param.default is not None:
                    clean[name] = param.check(param.default)
                continue
            clean[name] = param.check(args[name])
        return clean

    def target_host(self, args: dict[str, Any]) -> str:
        return _subst(self.run_on, args) if "{" in self.run_on else self.run_on

    def render(self, args: dict[str, Any], host_init: str = "systemd") -> tuple[str, list[tuple[str, list[str]]]]:
        """Return (host, [(label, argv)]) for validated ``args``.

        ``host_init`` is the target host's init system (``[hosts.X] init``);
        commands declaring ``init = [...]`` run only on matching hosts.
        """
        host = self.target_host(args)
        if host not in self.hosts and host != "local":
            raise ToolError(f"{self.name} may not run on {host}; allowed: {', '.join(self.hosts)}")
        rendered = []
        for command in self.commands:
            if command.hosts and host not in command.hosts:
                continue
            if command.init and host_init not in command.init:
                continue
            argv: list[str] = []
            for element in command.argv:
                if isinstance(element, dict):
                    gate = element.get("when", "")
                    if args.get(gate) in (None, False, ""):
                        continue
                    argv.extend(_subst(str(e), args) for e in element.get("argv", []))
                else:
                    argv.append(_subst(str(element), args))
            rendered.append((command.label or shlex.join(argv), argv))
        if not rendered:
            raise ToolError(f"{self.name} has no command for host {host}")
        return host, rendered


def _subst(template: str, args: dict[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in ("{{", "}}"):
            return token[0]
        name = match.group(1)
        if name.startswith("@"):
            if name not in BUILTINS:
                raise ToolError(f"unknown builtin {name}")
            return BUILTINS[name]
        if args.get(name) is None:
            raise ToolError(f"missing value for {{{name}}} (make it required or put it in a when-group)")
        value = args[name]
        return ("true" if value else "false") if isinstance(value, bool) else str(value)

    return PLACEHOLDER_RE.sub(repl, template)


def load_tool(path: Path, known_hosts: list[str]) -> Tool:
    data = tomllib.loads(path.read_text())
    name = data.get("name", path.stem)
    if not NAME_RE.match(name):
        raise ToolError(f"{path.name}: bad tool name {name!r}")
    safety = data.get("safety")
    if safety not in SAFETY:
        raise ToolError(f"{path.name}: safety must be one of {SAFETY}")
    params = {pname: Param(name=pname, **spec) for pname, spec in data.get("params", {}).items()}
    run_on = data.get("run_on", "local")
    hosts = list(data.get("hosts", ["local"]))
    if "*" in hosts:  # any configured host
        hosts = [h for h in hosts if h != "*"] + [h for h in known_hosts if h not in hosts]
    if run_on == "{host}":
        params.setdefault(
            "host", Param(name="host", required=True, enum=hosts, description="Host to run on.")
        )
    elif run_on not in hosts and run_on != "local":
        raise ToolError(f"{path.name}: run_on {run_on!r} not in hosts")
    for host in hosts:
        if host != "local" and host not in known_hosts:
            raise ToolError(f"{path.name}: unknown host {host!r}")
    commands = [
        Command(argv=c["argv"], hosts=c.get("hosts"), init=c.get("init"), label=c.get("label", ""))
        for c in data.get("commands", [])
    ]
    if not commands:
        raise ToolError(f"{path.name}: no [[commands]]")
    tool = Tool(
        name=name,
        description=data["description"].strip(),
        safety=safety,
        run_on=run_on,
        hosts=hosts,
        commands=commands,
        params=params,
        timeout=int(data.get("timeout", 60)),
        max_output=int(data.get("max_output", 12000)),
        cwd=data.get("cwd"),
        env=dict(data.get("env", {})),
        source=str(path),
    )
    # Fail at load time on templates that reference undeclared parameters.
    for command in commands:
        for element in command.argv:
            parts = element.get("argv", []) + [element.get("when", "")] if isinstance(element, dict) else [element]
            for part in parts:
                for match in PLACEHOLDER_RE.finditer(str(part)):
                    ref = match.group(1)
                    if ref and not ref.startswith("@") and ref not in params:
                        raise ToolError(f"{path.name}: argv references undeclared parameter {ref!r}")
    return tool


def load_tools(directory: Path, known_hosts: list[str]) -> dict[str, Tool]:
    tools = {}
    for path in sorted(directory.glob("*.toml")):
        tool = load_tool(path, known_hosts)
        if tool.name in tools:
            raise ToolError(f"duplicate tool name {tool.name}")
        tools[tool.name] = tool
    return tools


@dataclass
class RunResult:
    host: str
    exit_codes: list[int]
    output: str
    truncated: bool
    seconds: float

    @property
    def ok(self) -> bool:
        return all(code == 0 for code in self.exit_codes)


def wrap_for_host(argv: list[str], host_cfg: dict[str, Any], env: dict[str, str]) -> list[str]:
    """Turn a command argv into the argv that runs it on the configured host."""
    transport = host_cfg.get("transport", "local")
    if env:
        argv = ["env", *(f"{k}={v}" for k, v in env.items()), *argv]
    if transport == "local":
        return argv
    if transport == "ssh":
        return [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-T",
            str(host_cfg["ssh"]), "--", shlex.join(argv),
        ]
    raise ToolError(f"unsupported transport {transport!r}")


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = limit // 3
    tail = limit - head
    return text[:head] + f"\n... [{len(text) - limit} characters truncated] ...\n" + text[-tail:], True


def execute(tool: Tool, args: dict[str, Any], hosts: dict[str, dict[str, Any]], this_host: str) -> RunResult:
    target = tool.target_host(args)
    host_cfg = hosts.get(target) or ({"transport": "local"} if target in ("local", this_host) else None)
    if host_cfg is None:
        raise ToolError(f"host {target} is not configured")
    if target == this_host:
        host_cfg = {**host_cfg, "transport": "local"}
    host, commands = tool.render(args, str(host_cfg.get("init", "systemd")))
    env = {k: _subst(v, args) for k, v in tool.env.items()}
    cwd = _subst(tool.cwd, args) if tool.cwd else None
    if cwd and host_cfg.get("transport") != "local":
        raise ToolError("cwd is only supported for local tools")
    started = time.monotonic()
    deadline = started + tool.timeout
    chunks, codes = [], []
    for label, argv in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            chunks.append(f"$ {label}\n[skipped: tool timeout of {tool.timeout}s reached]\n")
            codes.append(124)
            break
        full = wrap_for_host(argv, host_cfg, env)
        try:
            proc = subprocess.run(
                full, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=remaining, cwd=cwd, check=False,
            )
            out = proc.stdout.decode(errors="replace")
            codes.append(proc.returncode)
            status = "" if proc.returncode == 0 else f"[exit {proc.returncode}]\n"
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode(errors="replace")
            codes.append(124)
            status = f"[timed out after {tool.timeout}s]\n"
        except FileNotFoundError as exc:
            out, status = "", f"[not found: {exc.filename}]\n"
            codes.append(127)
        prefix = f"$ {label}\n" if len(commands) > 1 else ""
        chunks.append(prefix + out + status)
    output, truncated = truncate("\n".join(chunks), tool.max_output)
    return RunResult(host, codes, output, truncated, round(time.monotonic() - started, 2))
