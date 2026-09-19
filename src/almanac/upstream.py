"""Companion MCP servers the local agent can use (e.g. XivMcp inside FFXIV).

``[upstreams.<name>]`` in the config declares a streamable-HTTP MCP server.
The agent (``almanac ask``/``run``/``chat``) lists its tools once per run and
offers them to the local model as ``<name>__<tool>``. Nothing here is exposed
through almanac's own MCP server: clients like Claude Code connect to a
companion server directly.

Credentials are read **at call time** from the place the upstream itself keeps
them (``token_json`` + ``token_key``, ``token_file`` or ``token_env``); they
are never copied into almanac's config, logs, audit log or notes.

Tiers: if the upstream labels tools with a tier (``tier_meta`` names the
``_meta`` key, e.g. ``dev.xivmcp/permission``), only ``free_tiers`` are
offered by default. Other tiers (XivMcp: action, chat) are offered only when
the user explicitly allows game actions for this run; the upstream's own
confirmation (XivMcp's in-game window) still applies and is never bypassed.

If the server is not reachable (game or plugin not running) the agent carries
on without it and says so.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

SEP = "__"


class UpstreamError(RuntimeError):
    pass


@dataclass
class UpstreamTool:
    upstream: str
    name: str
    description: str
    input_schema: dict[str, Any]
    tier: str

    @property
    def qualified(self) -> str:
        return f"{self.upstream}{SEP}{self.name}"


@dataclass
class Upstream:
    name: str
    url: str
    token_json: str = ""
    token_key: str = ""
    token_file: str = ""
    token_env: str = ""
    tier_meta: str = ""
    free_tiers: list[str] = field(default_factory=lambda: ["read"])
    status_tool: str = ""
    status_agent: str = "almanac"
    # Optional: offer only tools whose _meta[category_meta] is in categories
    # (keeps the tool list small enough for a small local model).
    category_meta: str = ""
    categories: list[str] = field(default_factory=list)
    timeout: float = 30.0

    @classmethod
    def from_config(cls, name: str, raw: dict[str, Any]) -> "Upstream":
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(name=name, **known)

    def token(self) -> str | None:
        """Read the bearer token now, from wherever the upstream keeps it."""
        if self.token_env:
            return os.environ.get(self.token_env) or None
        if self.token_file:
            path = Path(self.token_file).expanduser()
            return path.read_text().strip() if path.is_file() else None
        if self.token_json:
            path = Path(self.token_json).expanduser()
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            value = data.get(self.token_key) if isinstance(data, dict) else None
            return str(value) if value else None
        return None

    def _headers(self) -> dict[str, str]:
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _session_call(self, method: str, *args: Any) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.types import Implementation

        from . import __version__

        async with streamablehttp_client(self.url, headers=self._headers(), timeout=self.timeout) as (read, write, _):
            async with ClientSession(read, write, client_info=Implementation(name="almanac", version=__version__)) as session:
                await session.initialize()
                return await getattr(session, method)(*args)

    def _run(self, method: str, *args: Any) -> Any:
        try:
            return asyncio.run(asyncio.wait_for(self._session_call(method, *args), self.timeout + 5))
        except (httpx.ConnectError, ConnectionRefusedError) as exc:
            raise UpstreamError(f"{self.name} is not reachable at {self.url} (is it running?)") from exc
        except BaseException as exc:  # anyio wraps errors in ExceptionGroups
            root = exc
            while isinstance(root, BaseExceptionGroup) and root.exceptions:
                root = root.exceptions[0]
            if isinstance(root, (httpx.ConnectError, ConnectionRefusedError, OSError)):
                raise UpstreamError(f"{self.name} is not reachable at {self.url} (is it running?)") from exc
            if isinstance(root, httpx.HTTPStatusError) and root.response.status_code == 401:
                raise UpstreamError(f"{self.name} rejected the token (check {self.token_json or self.token_file or self.token_env})") from exc
            if isinstance(root, (KeyboardInterrupt, SystemExit)):
                raise
            raise UpstreamError(f"{self.name}: {root.__class__.__name__}: {root}") from exc

    def list_tools(self) -> list[UpstreamTool]:
        result = self._run("list_tools")
        tools = []
        for tool in result.tools:
            meta = tool.meta or {}
            tier = str(meta.get(self.tier_meta, "")) if self.tier_meta else ""
            if not tier:
                tier = "read" if tool.annotations and tool.annotations.readOnlyHint else "unknown"
            category = str(meta.get(self.category_meta, "")) if self.category_meta else ""
            if self.categories and category not in self.categories and tool.name != self.status_tool:
                continue
            tools.append(UpstreamTool(self.name, tool.name, tool.description or "", tool.inputSchema, tier.lower()))
        return tools

    def call(self, tool: str, args: dict[str, Any]) -> tuple[str, bool]:
        result = self._run("call_tool", tool, args)
        parts = []
        for item in result.content:
            parts.append(getattr(item, "text", None) or json.dumps(item.model_dump(), default=str)[:2000])
        return "\n".join(parts), bool(result.isError)


class Companions:
    """All configured upstreams for one agent run: discovery, calls, status posts."""

    def __init__(self, config_upstreams: dict[str, dict[str, Any]], allow_actions: bool = False) -> None:
        self.upstreams = {name: Upstream.from_config(name, raw) for name, raw in config_upstreams.items()}
        self.allow_actions = allow_actions
        self.tools: dict[str, UpstreamTool] = {}
        self.notes: list[str] = []
        self._status_ok: dict[str, bool] = {}

    def discover(self) -> None:
        for up in self.upstreams.values():
            try:
                tools = up.list_tools()
            except UpstreamError as exc:
                self.notes.append(f"{exc}; its tools are unavailable in this run.")
                continue
            self._status_ok[up.name] = bool(up.status_tool) and any(t.name == up.status_tool for t in tools)
            # The status tool is driven by almanac itself, not offered to the model.
            tools = [t for t in tools if t.name != up.status_tool]
            offered = [t for t in tools if t.tier in up.free_tiers or self.allow_actions]
            held = len(tools) - len(offered)
            for tool in offered:
                self.tools[tool.qualified] = tool
            if held:
                self.notes.append(
                    f"{up.name}: {held} action/chat tools withheld (not requested). Ask with game actions allowed to use them."
                )

    def functions(self) -> list[dict[str, Any]]:
        out = []
        for tool in self.tools.values():
            tier = f"[{tool.upstream} {tool.tier}] "
            confirm = " The player confirms this in game." if tool.tier not in self.upstreams[tool.upstream].free_tiers else ""
            out.append(
                {
                    "type": "function",
                    "function": {"name": tool.qualified, "description": (tier + tool.description + confirm)[:1024], "parameters": tool.input_schema},
                }
            )
        return out

    def call(self, qualified: str, args: dict[str, Any]) -> str:
        tool = self.tools.get(qualified)
        if tool is None:
            return f"error: {qualified} is not available in this run"
        try:
            text, is_error = self.upstreams[tool.upstream].call(tool.name, args)
        except UpstreamError as exc:
            return f"error: {exc}"
        return ("error: " if is_error else "") + text

    def post_status(self, status: str, state: str = "running", progress: float | None = None, detail: str | None = None) -> None:
        """Best effort: show progress on every upstream that has a status tool (XivMcp's agent board)."""
        for up in self.upstreams.values():
            if not self._status_ok.get(up.name):
                continue
            args: dict[str, Any] = {"agent": up.status_agent, "status": status[:200], "state": state}
            if progress is not None:
                args["progress"] = max(0.0, min(1.0, progress))
            if detail:
                args["detail"] = detail[:2000]
            try:
                up.call(up.status_tool, args)
            except UpstreamError:
                self._status_ok[up.name] = False
