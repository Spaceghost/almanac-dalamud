"""MCP server: the knowledge base and every declared tool, over stdio or HTTP.

* stdio: for a local client that spawns ``almanac mcp --stdio``.
* streamable HTTP at ``/mcp`` on the addresses in ``[mcp] listen``, every
  request authenticated with the shared bearer token.

change/destructive tools: if the client advertised the MCP *elicitation*
capability the server asks the human directly (form with one "approve"
checkbox, showing the exact plan). Otherwise the first call returns the plan
and a token, and the model must show the plan and call again with
``confirm=<token>``. Either way ``service.Almanac.call`` makes the decision
and writes the audit log.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Receive, Scope, Send

from .service import Almanac

log = logging.getLogger("almanac.mcp")

INSTRUCTIONS = """almanac: knowledge and tools for the machines it is configured for.
Before acting on a host or project, kb_search for it and kb_read the relevant
notes and runbooks. Tools are labelled [read], [change] or [destructive].
change/destructive tools do not run on the first call: show the returned plan
to the user and only repeat the call with the confirm token if they approve.
When you learn something durable (a fix, a path, a gotcha), propose it with
kb_note. Never put secrets in notes; reference the file that holds them."""


def build_server(almanac: Almanac, transport: str) -> Server:
    server: Server = Server("almanac", version="0.2.0", instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=spec["name"],
                description=spec["description"],
                inputSchema=spec["input_schema"],
                annotations=types.ToolAnnotations(
                    readOnlyHint=spec["safety"] == "read",
                    destructiveHint=spec["safety"] == "destructive",
                ),
            )
            for spec in almanac.catalogue()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        ctx = server.request_context
        session = ctx.session
        params = session.client_params
        client = params.clientInfo.name if params else "unknown"
        caller = f"mcp-{transport}:{client}"
        outcome = await anyio.to_thread.run_sync(almanac.call, name, arguments or {}, caller)
        caps = params.capabilities if params else None
        if outcome.needs_confirmation and caps is not None and caps.elicitation is not None:
            try:
                answer = await session.elicit(
                    message=f"almanac wants to run a {almanac.safety_of(name)} action:\n\n{outcome.plan}\n\nApprove?",
                    requestedSchema={
                        "type": "object",
                        "properties": {"approve": {"type": "boolean", "title": "Approve and run", "default": False}},
                        "required": ["approve"],
                    },
                )
                approved = answer.action == "accept" and bool((answer.content or {}).get("approve"))
            except Exception as exc:  # client claimed the capability but failed: fall back to the token
                log.warning("elicitation failed (%s); falling back to confirm token", exc)
            else:
                if not approved:
                    almanac.audit("declined", name, dict(arguments or {}), caller, via="elicitation")
                    return types.CallToolResult(content=[types.TextContent(type="text", text="The user declined. Nothing was run.")])
                outcome = await anyio.to_thread.run_sync(lambda: almanac.call(name, arguments or {}, caller, approved=True))
        return types.CallToolResult(content=[types.TextContent(type="text", text=outcome.text)], isError=outcome.is_error)

    return server


async def serve_stdio(almanac: Almanac) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(almanac, "stdio")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


class BearerMCPApp:
    """ASGI app: /mcp behind a bearer token, plus an unauthenticated /healthz."""

    def __init__(self, manager: StreamableHTTPSessionManager, token: str, tool_count: int) -> None:
        self.manager = manager
        self.token = token.encode()
        self.tool_count = tool_count

    async def _reply(self, send: Send, status: int, body: dict[str, Any], extra: list[tuple[bytes, bytes]] | None = None) -> None:
        payload = json.dumps(body).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()), *(extra or [])]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            async with self.manager.run():
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                message = await receive()
                if message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
            return
        if scope["type"] != "http":
            return
        path = scope["path"].rstrip("/")
        if path == "/healthz":
            await self._reply(send, 200, {"ok": True, "tools": self.tool_count})
            return
        if path != "/mcp":
            await self._reply(send, 404, {"error": "not found"})
            return
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"")
        supplied = auth[7:] if auth.lower().startswith(b"bearer ") else b""
        if not supplied or not hmac.compare_digest(supplied, self.token):
            await self._reply(send, 401, {"error": "missing or wrong bearer token"}, [(b"www-authenticate", b"Bearer")])
            return
        await self.manager.handle_request(scope, receive, send)


def http_app(almanac: Almanac) -> BearerMCPApp:
    server = build_server(almanac, "http")
    manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=False)
    return BearerMCPApp(manager, almanac.config.read_token(), len(almanac.catalogue()))
