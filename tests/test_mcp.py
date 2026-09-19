"""MCP server end to end over in-memory streams (no network)."""

import anyio
import mcp.types as types
from mcp.shared.memory import create_connected_server_and_client_session

from almanac.mcp_server import build_server
from almanac.service import Almanac


def run(coro_fn):
    return anyio.run(coro_fn)


def test_list_and_read_tool(config) -> None:
    server = build_server(Almanac(config), "test")

    async def go():
        async with create_connected_server_and_client_session(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert {"kb_search", "kb_read", "kb_note", "echo_tool", "touch_tool"} <= set(tools)
            assert tools["echo_tool"].annotations.readOnlyHint
            result = await client.call_tool("echo_tool", {"word": "hey"})
            assert not result.isError and "hey" in result.content[0].text
            result = await client.call_tool("kb_search", {"query": "incus"})
            assert "hosts/example-host.md" in result.content[0].text

    run(go)


def test_change_without_elicitation_returns_plan_then_token_runs(config, tmp_path) -> None:
    server = build_server(Almanac(config), "test")

    async def go():
        async with create_connected_server_and_client_session(server) as client:
            first = await client.call_tool("touch_tool", {"name": "viamcp"})
            text = first.content[0].text
            assert text.startswith("NOT RUN") and not (tmp_path / "viamcp").exists()
            token = text.rsplit('confirm="', 1)[1].split('"')[0]
            done = await client.call_tool("touch_tool", {"name": "viamcp", "confirm": token})
            assert not done.isError and (tmp_path / "viamcp").exists()

    run(go)


def test_change_with_elicitation(config, tmp_path) -> None:
    server = build_server(Almanac(config), "test")
    answers = iter([False, True])

    async def elicit(context, params):
        assert "touch" in params.message
        return types.ElicitResult(action="accept", content={"approve": next(answers)})

    async def go():
        async with create_connected_server_and_client_session(server, elicitation_callback=elicit) as client:
            declined = await client.call_tool("touch_tool", {"name": "elicited"})
            assert "declined" in declined.content[0].text and not (tmp_path / "elicited").exists()
            approved = await client.call_tool("touch_tool", {"name": "elicited"})
            assert not approved.isError and (tmp_path / "elicited").exists()

    run(go)
