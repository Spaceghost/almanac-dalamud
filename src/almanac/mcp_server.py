"""MCP server: the knowledge base and every declared tool, over stdio or HTTP.

* stdio: for a local client that spawns ``almanac mcp --stdio``.
* streamable HTTP at ``/mcp`` on the addresses in ``[mcp] listen``, every
  request authenticated with the shared bearer token.

change/destructive tools: if the client advertised the MCP *elicitation*
capability the server asks the human directly (form with one "approve"
checkbox, showing the exact plan):

* handshake-era sessions (protocol up to 2025-11-25): an ``elicitation/create``
  request to the client while the call waits, for at most ``ELICIT_TIMEOUT``;
* 2026-07-28 sessions, which forbid server-initiated requests: the call
  returns an ``InputRequiredResult`` carrying the same form and an opaque
  ``request_state``. The client asks the human and retries with the answer.
  The state is sealed by the SDK's ``RequestStateBoundary`` (bound to the tool
  and its arguments, expiring) and is also a single-use server-side nonce, so
  one approval runs the call at most once.

Otherwise (no elicitation, or the elicitation failed or timed out) the first
call returns the plan and a token, and the model must show the plan and call
again with ``confirm=<token>``. Either way ``service.Almanac.call`` makes the
decision and writes the audit log.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from typing import Any

import anyio
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.request_state import RequestStateBoundary, RequestStateSecurity
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types.version import MODERN_PROTOCOL_VERSIONS
from starlette.types import Receive, Scope, Send

from . import __version__
from .service import Almanac, Outcome

log = logging.getLogger("almanac.mcp")

INSTRUCTIONS = """almanac: knowledge and tools for the machines it is configured for.
Before acting on a host or project, kb_search for it and kb_read the relevant
notes and runbooks. Tools are labelled [read], [change] or [destructive].
change/destructive tools do not run on the first call: show the returned plan
to the user and only repeat the call with the confirm token if they approve.
When you learn something durable (a fix, a path, a gotcha), propose it with
kb_note. Never put secrets in notes; reference the file that holds them."""


# How long a human has to answer an approval form. On a timeout nothing runs.
ELICIT_TIMEOUT = 600.0
# The input_requests key of the approval form on 2026-07-28 sessions.
APPROVAL_KEY = "almanac_approval"
APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"approve": {"type": "boolean", "title": "Approve and run", "default": False}},
    "required": ["approve"],
}
DECLINED = "The user declined. Nothing was run."
STALE = "This approval expired or was already used. Nothing was run. Call the tool again to ask the user."


class PendingApprovals:
    """Single-use approval nonces for the 2026-07-28 input-required round trip.

    A nonce is minted when the approval form is sent, bound to exactly one
    (tool, arguments, caller) and ``ttl`` seconds; ``take`` consumes it whether
    or not it matches, so an answer can be used at most once.
    """

    def __init__(self, ttl: float = ELICIT_TIMEOUT, limit: int = 256) -> None:
        self.ttl = ttl
        self.limit = limit
        self._pending: dict[str, tuple[str, str, float]] = {}

    @staticmethod
    def _binding(name: str, args: dict[str, Any], caller: str) -> str:
        return json.dumps([name, args, caller], sort_keys=True, default=str)

    def mint(self, name: str, args: dict[str, Any], caller: str) -> str:
        now = time.monotonic()
        self._pending = {k: v for k, v in self._pending.items() if v[2] > now}
        while len(self._pending) >= self.limit:
            self._pending.pop(next(iter(self._pending)))
        nonce = secrets.token_urlsafe(32)
        self._pending[nonce] = (name, self._binding(name, args, caller), now + self.ttl)
        return nonce

    def take(self, nonce: str, name: str, args: dict[str, Any], caller: str) -> bool:
        entry = self._pending.pop(nonce, None)
        if entry is None:
            return False
        _, binding, deadline = entry
        return time.monotonic() < deadline and hmac.compare_digest(binding, self._binding(name, args, caller))


def _approved(answer: object) -> bool:
    """Only an explicit accept with approve=true counts; anything else is a no."""
    if isinstance(answer, dict):
        try:
            answer = types.ElicitResult.model_validate(answer)
        except ValueError:
            return False
    if not isinstance(answer, types.ElicitResult) or answer.action != "accept":
        return False
    return (answer.content or {}).get("approve") is True


def _result(outcome: Outcome) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=outcome.text)], is_error=outcome.is_error)


def _text(text: str, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


def build_server(almanac: Almanac, transport: str) -> Server:
    pending = PendingApprovals(ELICIT_TIMEOUT)

    async def list_tools(ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=spec["name"],
                    description=spec["description"],
                    input_schema=spec["input_schema"],
                    annotations=types.ToolAnnotations(
                        read_only_hint=spec["safety"] == "read",
                        destructive_hint=spec["safety"] == "destructive",
                    ),
                )
                for spec in almanac.catalogue()
            ]
        )

    async def decide(name: str, arguments: dict[str, Any], caller: str, approved: bool) -> types.CallToolResult:
        if not approved:
            almanac.audit("declined", name, dict(arguments), caller, via="elicitation")
            return _text(DECLINED)
        outcome = await anyio.to_thread.run_sync(lambda: almanac.call(name, arguments, caller, approved=True))
        return _result(outcome)

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        name = params.name
        arguments = dict(params.arguments or {})
        session = ctx.session
        info = session.client_params.client_info if session.client_params else None
        caller = f"mcp-{transport}:{info.name if info else 'unknown'}"

        if params.request_state is not None:
            # A retry carrying the human's answer. The boundary has already
            # checked the seal, expiry and tool/arguments binding; the nonce
            # makes the answer single-use.
            if not pending.take(params.request_state, name, arguments, caller):
                almanac.audit("refused", name, arguments, caller, via="elicitation", reason="stale approval")
                return _text(STALE, is_error=True)
            answer = (params.input_responses or {}).get(APPROVAL_KEY)
            return await decide(name, arguments, caller, _approved(answer))

        outcome = await anyio.to_thread.run_sync(almanac.call, name, arguments, caller)
        caps = session.client_capabilities
        if not outcome.needs_confirmation or caps is None or caps.elicitation is None:
            return _result(outcome)
        message = f"almanac wants to run a {almanac.safety_of(name)} action:\n\n{outcome.plan}\n\nApprove?"

        if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
            form = types.ElicitRequest(params=types.ElicitRequestFormParams(message=message, requested_schema=APPROVAL_SCHEMA))
            return types.InputRequiredResult(input_requests={APPROVAL_KEY: form}, request_state=pending.mint(name, arguments, caller))

        try:
            with anyio.fail_after(ELICIT_TIMEOUT):
                answer = await session.elicit_form(message=message, requested_schema=APPROVAL_SCHEMA, related_request_id=ctx.request_id)
        except Exception as exc:  # client claimed the capability but failed or timed out: fall back to the token
            log.warning("elicitation failed (%r); falling back to confirm token", exc)
            return _result(outcome)
        return await decide(name, arguments, caller, _approved(answer))

    server: Server = Server(
        "almanac",
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    # Seal the 2026-07-28 request_state: bound to the tool and its arguments,
    # expiring with the approval, unreadable and unforgeable by the client.
    server.middleware.append(RequestStateBoundary(RequestStateSecurity.ephemeral(ttl=ELICIT_TIMEOUT), default_audience="almanac"))
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
