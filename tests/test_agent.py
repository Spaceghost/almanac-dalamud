import json

from almanac.agent import Agent, runbook_agent
from almanac.service import Almanac


def scripted(replies):
    calls = []

    def post(payload, on_text=None):
        calls.append(payload)
        return replies.pop(0)

    return post, calls


def tool_call(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_agent_runs_read_tools_and_answers(config) -> None:
    post, calls = scripted([tool_call("echo_tool", {"word": "ping"}), {"role": "assistant", "content": "Saw ping."}])
    transcript = Agent(Almanac(config), post=post).run("echo ping")
    assert transcript.answer == "Saw ping."
    assert "ping" in calls[1]["messages"][-1]["content"]
    offered = {f["function"]["name"] for f in calls[0]["tools"]}
    assert "touch_tool" not in offered and "kb_note" not in offered


def test_agent_change_needs_approval(config, tmp_path) -> None:
    post, _ = scripted([tool_call("touch_tool", {"name": "nope"}), {"role": "assistant", "content": "done"}])
    Agent(Almanac(config), allow_change=True, approver=lambda n, p: False, post=post).run("x")
    assert not (tmp_path / "nope").exists()


def test_runbook_preapproval(config, tmp_path) -> None:
    alm = Almanac(config)
    agent, text = runbook_agent(alm, "check", allow_change=False, approver=lambda n, p: False)
    assert "Run echo_tool." in text
    assert {t["name"] for t in agent.catalogue} >= {"echo_tool", "kb_search"}
    assert "touch_tool" not in {t["name"] for t in agent.catalogue}  # not in the runbook's tools list


def test_parse_sse_chat_folds_text_and_tool_calls() -> None:
    from almanac.agent import parse_sse_chat

    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "kb_", "arguments": '{"q'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "search", "arguments": '":1}'}}]}}]},
    ]
    seen = []
    msg = parse_sse_chat(iter([f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]), seen.append)
    assert msg["content"] == "Hello" and seen == ["Hel", "lo"]
    assert msg["tool_calls"][0]["function"] == {"name": "kb_search", "arguments": '{"q":1}'}


class FakeUpstream:
    def __init__(self, tools, reachable=True):
        self.tools, self.reachable, self.calls = tools, reachable, []


def make_companions(monkeypatch, reachable=True, allow_actions=False):
    from almanac import upstream as up

    tools = [
        up.UpstreamTool("xivmcp", "get_player", "Player info", {"type": "object"}, "read"),
        up.UpstreamTool("xivmcp", "echo_chat", "Echo", {"type": "object"}, "ui"),
        up.UpstreamTool("xivmcp", "teleport", "Teleport", {"type": "object"}, "action"),
        up.UpstreamTool("xivmcp", "post_status", "Board", {"type": "object"}, "ui"),
    ]
    calls = []

    def list_tools(self):
        if not reachable:
            raise up.UpstreamError("xivmcp is not reachable at http://127.0.0.1:1/mcp (is it running?)")
        return tools

    def call(self, name, args):
        calls.append((name, args))
        return f"ok {name}", False

    monkeypatch.setattr(up.Upstream, "list_tools", list_tools)
    monkeypatch.setattr(up.Upstream, "call", call)
    comp = up.Companions(
        {"xivmcp": {"url": "http://127.0.0.1:1/mcp", "tier_meta": "dev.xivmcp/permission", "free_tiers": ["read", "ui"], "status_tool": "post_status"}},
        allow_actions=allow_actions,
    )
    comp.discover()
    return comp, calls


def test_companion_tiers_and_status(monkeypatch, config) -> None:
    comp, calls = make_companions(monkeypatch)
    assert set(comp.tools) == {"xivmcp__get_player", "xivmcp__echo_chat"}  # action withheld, post_status internal
    assert any("withheld" in n for n in comp.notes)
    post, _ = scripted([tool_call("xivmcp__get_player", {}), {"role": "assistant", "content": "You are level 100."}])
    transcript = Agent(Almanac(config), post=post, companions=comp).run("who am I")
    assert transcript.answer == "You are level 100."
    names = [c[0] for c in calls]
    assert names[0] == "post_status" and "get_player" in names and names[-1] == "post_status"
    assert calls[-1][1]["state"] == "done"
    comp, _ = make_companions(monkeypatch, allow_actions=True)
    assert "xivmcp__teleport" in comp.tools


def test_companion_unreachable_degrades(monkeypatch, config) -> None:
    comp, calls = make_companions(monkeypatch, reachable=False)
    assert comp.tools == {} and "not reachable" in comp.notes[0]
    post, payloads = scripted([{"role": "assistant", "content": "fine"}])
    agent = Agent(Almanac(config), post=post, companions=comp)
    assert agent.run("x").answer == "fine" and calls == []
    assert "not reachable" in payloads[0]["messages"][0]["content"]


def test_upstream_token_read_at_call_time(tmp_path) -> None:
    from almanac.upstream import Upstream

    cfg = tmp_path / "plugin.json"
    up = Upstream(name="x", url="http://127.0.0.1:1/mcp", token_json=str(cfg), token_key="BearerToken")
    assert up.token() is None
    cfg.write_text('﻿{"BearerToken": "abc"}', encoding="utf-8")
    assert up.token() == "abc"
