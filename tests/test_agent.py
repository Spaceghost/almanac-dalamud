import json

from almanac.agent import Agent, runbook_agent
from almanac.service import Almanac


def scripted(replies):
    calls = []

    def post(payload):
        calls.append(payload)
        return {"choices": [{"message": replies.pop(0)}]}

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
