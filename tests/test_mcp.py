"""MCP server end to end, in process (no network) and over loopback HTTP.

The approval path is the point: a change or destructive tool must never run
without the human's explicit approval, one approval runs exactly one call, and
read tools never ask. Every case runs on both protocol eras mcp 2.x speaks:
"legacy" (initialize handshake; the server sends elicitation/create while the
call waits) and "2026-07-28" (no server-initiated requests; the call returns
an InputRequiredResult and the client retries with the answer).
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
import uvicorn
from mcp import Client, types
from mcp.shared.exceptions import MCPError

from almanac import mcp_server
from almanac.mcp_server import APPROVAL_KEY, PendingApprovals, build_server, http_app
from almanac.service import Almanac
from almanac.upstream import Upstream, UpstreamError

MODES = ["legacy", "2026-07-28"]
MODERN = "2026-07-28"


def run(coro_fn):
    return anyio.run(coro_fn)


@pytest.fixture
def almanac(config, tmp_path: Path) -> Almanac:
    """The shared config plus a destructive tool (rm -f of a file under tmp_path)."""
    (config.tools_dirs[0] / "wipe_tool.toml").write_text(
        'description = "Delete a file."\nsafety = "destructive"\nrun_on = "local"\n'
        '[params.name]\ntype = "string"\npattern = "[a-z]+"\nrequired = true\n'
        f'[[commands]]\nargv = ["rm", "-f", "{tmp_path}/{{name}}"]\n'
    )
    return Almanac(config)


def audit(almanac: Almanac, event: str, tool: str) -> list[dict[str, Any]]:
    if not almanac.audit_path.exists():
        return []
    records = [json.loads(line) for line in almanac.audit_path.read_text().splitlines()]
    return [r for r in records if r["event"] == event and r["tool"] == tool]


class Human:
    """An elicitation callback: answers from a script and records every question."""

    def __init__(self, *answers: types.ElicitResult | types.ErrorData | float) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    async def __call__(self, context, params) -> types.ElicitResult | types.ErrorData:
        self.asked.append(params.message)
        answer = self.answers.pop(0)
        if isinstance(answer, float):  # a human who never answers
            await anyio.sleep(answer)
            pytest.fail("the server should have given up waiting")
        return answer


def approve() -> types.ElicitResult:
    return types.ElicitResult(action="accept", content={"approve": True})


REFUSALS = {
    "decline": types.ElicitResult(action="decline"),
    "cancel": types.ElicitResult(action="cancel"),
    "unticked": types.ElicitResult(action="accept", content={"approve": False}),
    "no-content": types.ElicitResult(action="accept"),
    "truthy-string": types.ElicitResult(action="accept", content={"approve": "yes"}),
}


def target(tool: str, tmp_path: Path) -> tuple[Path, bool]:
    """The file a call acts on, and whether it exists before the call."""
    path = tmp_path / ("madeit" if tool == "touch_tool" else "keepme")
    if tool == "wipe_tool":
        path.write_text("precious")
    return path, path.exists()


# -- read tools --------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_list_and_read_tools_never_ask(almanac, mode) -> None:
    server = build_server(almanac, "test")
    human = Human()  # any question raises IndexError

    async def go():
        async with Client(server, mode=mode, elicitation_callback=human) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert {"kb_search", "kb_read", "kb_note", "echo_tool", "touch_tool", "wipe_tool"} <= set(tools)
            assert tools["echo_tool"].annotations.read_only_hint and not tools["echo_tool"].annotations.destructive_hint
            assert not tools["touch_tool"].annotations.read_only_hint and not tools["touch_tool"].annotations.destructive_hint
            assert tools["wipe_tool"].annotations.destructive_hint
            assert "confirm" in tools["touch_tool"].input_schema["properties"]
            result = await client.call_tool("echo_tool", {"word": "hey"})
            assert not result.is_error and "hey" in result.content[0].text
            result = await client.call_tool("kb_search", {"query": "incus"})
            assert "hosts/example-host.md" in result.content[0].text

    run(go)
    assert human.asked == []
    assert len(audit(almanac, "ran", "echo_tool")) == 1


# -- no elicitation: plan, then the confirm token -----------------------------


@pytest.mark.parametrize("mode", MODES)
def test_change_without_elicitation_returns_plan_then_token_runs(almanac, tmp_path, mode) -> None:
    server = build_server(almanac, "test")

    async def go():
        async with Client(server, mode=mode) as client:
            first = await client.call_tool("touch_tool", {"name": "viamcp"})
            text = first.content[0].text
            assert text.startswith("NOT RUN") and not (tmp_path / "viamcp").exists()
            token = text.rsplit('confirm="', 1)[1].split('"')[0]
            wrong = await client.call_tool("touch_tool", {"name": "other", "confirm": token})
            assert wrong.content[0].text.startswith("NOT RUN") and not (tmp_path / "other").exists()
            done = await client.call_tool("touch_tool", {"name": "viamcp", "confirm": token})
            assert not done.is_error and (tmp_path / "viamcp").exists()

    run(go)
    assert [r["args"] for r in audit(almanac, "ran", "touch_tool")] == [{"name": "viamcp"}]


# -- elicitation: refusals ----------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool", ["touch_tool", "wipe_tool"])
@pytest.mark.parametrize("refusal", sorted(REFUSALS))
def test_change_and_destructive_refuse_without_approval(almanac, tmp_path, mode, tool, refusal) -> None:
    server = build_server(almanac, "test")
    path, existed = target(tool, tmp_path)
    human = Human(REFUSALS[refusal])

    async def go():
        async with Client(server, mode=mode, elicitation_callback=human) as client:
            result = await client.call_tool(tool, {"name": path.name})
            assert result.content[0].text == "The user declined. Nothing was run."

    run(go)
    assert len(human.asked) == 1 and path.name in human.asked[0]
    assert path.exists() == existed
    assert audit(almanac, "ran", tool) == [] and audit(almanac, "approved", tool) == []
    assert len(audit(almanac, "declined", tool)) == 1


@pytest.mark.parametrize("tool", ["touch_tool", "wipe_tool"])
def test_elicitation_timeout_refuses(almanac, tmp_path, monkeypatch, tool) -> None:
    monkeypatch.setattr(mcp_server, "ELICIT_TIMEOUT", 0.3)
    server = build_server(almanac, "test")
    path, existed = target(tool, tmp_path)
    human = Human(30.0)

    async def go():
        async with Client(server, mode="legacy", elicitation_callback=human) as client:
            with anyio.fail_after(10):
                result = await client.call_tool(tool, {"name": path.name})
            assert result.content[0].text.startswith("NOT RUN")

    run(go)
    assert human.asked and path.exists() == existed and audit(almanac, "ran", tool) == []


@pytest.mark.parametrize("tool", ["touch_tool", "wipe_tool"])
def test_elicitation_error_refuses(almanac, tmp_path, tool) -> None:
    server = build_server(almanac, "test")
    path, existed = target(tool, tmp_path)
    human = Human(types.ErrorData(code=-1, message="client broke"))

    async def go():
        async with Client(server, mode="legacy", elicitation_callback=human) as client:
            result = await client.call_tool(tool, {"name": path.name})
            assert result.content[0].text.startswith("NOT RUN")

    run(go)
    assert human.asked and path.exists() == existed and audit(almanac, "ran", tool) == []


def test_modern_approval_expires(almanac, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "ELICIT_TIMEOUT", 0.3)
    server = build_server(almanac, "test")

    async def go():
        async with Client(server, mode=MODERN, elicitation_callback=Human()) as client:
            asked = await client.session.call_tool("touch_tool", {"name": "late"}, allow_input_required=True)
            assert isinstance(asked, types.InputRequiredResult)
            await anyio.sleep(0.5)
            with pytest.raises(MCPError, match="Invalid or expired requestState"):
                await client.session.call_tool(
                    "touch_tool", {"name": "late"}, input_responses={APPROVAL_KEY: approve()},
                    request_state=asked.request_state, allow_input_required=True,
                )

    run(go)
    assert not (tmp_path / "late").exists() and audit(almanac, "ran", "touch_tool") == []


# -- elicitation: one approval, one run ----------------------------------------


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool", ["touch_tool", "wipe_tool"])
def test_approval_runs_exactly_one_call(almanac, tmp_path, mode, tool) -> None:
    server = build_server(almanac, "test")
    path, existed = target(tool, tmp_path)
    human = Human(approve(), REFUSALS["decline"])

    async def go():
        async with Client(server, mode=mode, elicitation_callback=human) as client:
            done = await client.call_tool(tool, {"name": path.name})
            assert not done.is_error and done.content[0].text.startswith("host: ")
            assert path.exists() != existed
            path.unlink() if path.exists() else path.write_text("again")
            again = await client.call_tool(tool, {"name": path.name})
            assert again.content[0].text == "The user declined. Nothing was run."

    run(go)
    assert len(human.asked) == 2  # the second call asked again instead of reusing the approval
    assert len(audit(almanac, "ran", tool)) == 1
    assert [r["via"] for r in audit(almanac, "approved", tool)] == ["interactive"]


def test_modern_approval_cannot_be_replayed_or_forged(almanac, tmp_path) -> None:
    server = build_server(almanac, "test")
    yes = {APPROVAL_KEY: approve()}

    async def go():
        async with Client(server, mode=MODERN, elicitation_callback=Human()) as client:
            call = client.session.call_tool
            # An unsolicited "approval" without the server's state is ignored: it asks.
            forged = await call("touch_tool", {"name": "once"}, input_responses=yes, allow_input_required=True)
            assert isinstance(forged, types.InputRequiredResult) and not (tmp_path / "once").exists()
            form = forged.input_requests[APPROVAL_KEY]
            assert form.method == "elicitation/create" and "once" in form.params.message

            # A made-up state is rejected before the handler runs.
            with pytest.raises(MCPError, match="Invalid or expired requestState"):
                await call("touch_tool", {"name": "once"}, input_responses=yes, request_state="v1.made-up", allow_input_required=True)
            # The state is bound to the arguments it was issued for.
            with pytest.raises(MCPError, match="Invalid or expired requestState"):
                await call("touch_tool", {"name": "other"}, input_responses=yes, request_state=forged.request_state, allow_input_required=True)
            # ...and to the tool.
            with pytest.raises(MCPError, match="Invalid or expired requestState"):
                await call("wipe_tool", {"name": "once"}, input_responses=yes, request_state=forged.request_state, allow_input_required=True)
            assert audit(almanac, "ran", "touch_tool") == [] and audit(almanac, "ran", "wipe_tool") == []

            # The real answer runs the call once.
            done = await call("touch_tool", {"name": "once"}, input_responses=yes, request_state=forged.request_state, allow_input_required=True)
            assert isinstance(done, types.CallToolResult) and not done.is_error and (tmp_path / "once").exists()
            (tmp_path / "once").unlink()
            # Replaying the same answer and state does not run it again.
            replay = await call("touch_tool", {"name": "once"}, input_responses=yes, request_state=forged.request_state, allow_input_required=True)
            assert isinstance(replay, types.CallToolResult) and replay.is_error
            assert "already used" in replay.content[0].text and not (tmp_path / "once").exists()

    run(go)
    assert len(audit(almanac, "ran", "touch_tool")) == 1
    assert len(audit(almanac, "refused", "touch_tool")) == 1


def test_pending_approvals_are_single_use_and_bound(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: now[0])
    pending = PendingApprovals(ttl=60)
    nonce = pending.mint("t", {"a": 1}, "c")
    assert pending.take(nonce, "t", {"a": 1}, "c")
    assert not pending.take(nonce, "t", {"a": 1}, "c")  # used
    for other in [("u", {"a": 1}, "c"), ("t", {"a": 2}, "c"), ("t", {"a": 1}, "d")]:
        nonce = pending.mint("t", {"a": 1}, "c")
        assert not pending.take(nonce, *other)
        assert not pending.take(nonce, "t", {"a": 1}, "c")  # a mismatch still consumed it
    nonce = pending.mint("t", {"a": 1}, "c")
    now[0] += 61
    assert not pending.take(nonce, "t", {"a": 1}, "c")  # expired
    assert not pending.take("never-minted", "t", {"a": 1}, "c")
    small = PendingApprovals(ttl=60, limit=2)
    first = small.mint("t", {}, "c")
    small.mint("t", {}, "c")
    small.mint("t", {}, "c")
    assert not small.take(first, "t", {}, "c")  # evicted, never grows past the limit


# -- streamable HTTP, through almanac's own upstream client --------------------


@pytest.fixture
def http_server(almanac):
    """almanac's bearer-protected HTTP app on a loopback port, in a thread."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(http_app(almanac), log_level="warning", lifespan="on"))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(10)


def test_upstream_over_http(almanac, http_server, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALMANAC_TEST_TOKEN", almanac.config.read_token())
    up = Upstream(name="self", url=http_server, token_env="ALMANAC_TEST_TOKEN", timeout=10)
    tools = {t.name: t for t in up.list_tools()}
    assert tools["echo_tool"].tier == "read" and tools["touch_tool"].tier == "unknown"
    assert "word" in tools["echo_tool"].input_schema["properties"]
    text, is_error = up.call("echo_tool", {"word": "overhttp"})
    assert not is_error and "overhttp" in text
    # This client has no elicitation: a change tool only plans.
    text, is_error = up.call("touch_tool", {"name": "viahttp"})
    assert text.startswith("NOT RUN") and not (tmp_path / "viahttp").exists()

    monkeypatch.setenv("ALMANAC_TEST_TOKEN", "wrong")
    with pytest.raises(UpstreamError, match="rejected the token"):
        up.list_tools()


def test_upstream_unreachable() -> None:
    with socket.socket() as sock:  # a loopback port with nothing listening
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    up = Upstream(name="gone", url=f"http://127.0.0.1:{port}/mcp", timeout=5)
    with pytest.raises(UpstreamError, match="not reachable"):
        up.list_tools()
