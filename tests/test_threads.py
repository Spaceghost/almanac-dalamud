import io
import json

import pytest

from almanac.agent import Agent
from almanac.chat import JsonLines, pick_thread, run_ask, run_threads, thread_turn
from almanac.service import Almanac
from almanac.threads import OLD_TOOL_CHARS, Thread, ThreadError, ThreadStore, trim_history, valid_id


def scripted(replies):
    calls = []

    def post(payload, on_text=None):
        calls.append(json.loads(json.dumps(payload)))
        reply = replies.pop(0)
        if on_text and reply.get("content"):
            on_text(reply["content"])
        return reply

    return post, calls


def tool_call(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_ids_are_checked(config) -> None:
    store = ThreadStore(config.state_dir)
    assert valid_id("20260101-120000-ab12") and valid_id("raid.notes")
    for bad in ("", "../x", "a/b", ".hidden", "x" * 65, "a..b"):
        assert not valid_id(bad)
        with pytest.raises(ThreadError):
            store.open(bad)


def test_follow_up_sees_earlier_turns_and_is_saved(config) -> None:
    alm = Almanac(config)
    store = ThreadStore(config.state_dir)
    thread = store.open("t1")
    post, calls = scripted([
        tool_call("echo_tool", {"word": "ping"}),
        {"role": "assistant", "content": "The host answered ping."},
        {"role": "assistant", "content": "It answered twice."},
    ])
    agent = Agent(alm, post=post)
    assert thread_turn(agent, store, thread, "does the host answer?") == "The host answered ping."
    loaded = store.load("t1")
    assert loaded.title == "does the host answer?" and loaded.turns() == 1
    assert [m["role"] for m in loaded.messages] == ["user", "assistant", "tool", "assistant"]

    agent = Agent(alm, post=post)  # a new process: only the file carries the context
    thread_turn(agent, store, store.load("t1"), "and how often?")
    sent = calls[-1]["messages"]
    assert sent[0]["role"] == "system"
    assert [m["role"] for m in sent[1:]] == ["user", "assistant", "tool", "assistant", "user"]
    assert sent[1]["content"] == "does the host answer?" and sent[-1]["content"] == "and how often?"
    assert store.load("t1").turns() == 2


def test_trim_keeps_whole_newest_turns() -> None:
    messages = []
    for i in range(10):
        messages += [
            {"role": "user", "content": f"question {i}"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": str(i), "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": str(i), "content": "x" * 5000},
            {"role": "assistant", "content": f"answer {i}"},
        ]
    kept = trim_history(messages, 8000)
    assert kept and kept[0]["role"] == "user" and kept[-1]["content"] == "answer 9"
    assert kept[0]["content"] != "question 0"
    assert all(len(m.get("content") or "") <= OLD_TOOL_CHARS + 10 for m in kept if m["role"] == "tool")
    # every tool result still follows its call
    for i, m in enumerate(kept):
        if m["role"] == "tool":
            assert kept[i - 1].get("tool_calls")
    # the newest turn survives even alone over budget
    assert trim_history(messages, 10)[0]["content"] == "question 9"
    assert trim_history([], 100) == []


def test_pick_thread_options(config) -> None:
    store = ThreadStore(config.state_dir)
    assert pick_thread(store, None, False, False) == (None, False)
    new, fresh = pick_thread(store, None, True, False)  # --continue with none: a new one
    assert fresh and new.id
    first = store.open("a")
    first.add_turn([{"role": "user", "content": "hi"}], "m")
    store.save(first)
    latest, fresh = pick_thread(store, None, True, False)
    assert latest.id == "a" and not fresh
    named, fresh = pick_thread(store, "b", False, False)
    assert named.id == "b" and fresh


def test_stream_json_ask_in_a_thread(config, monkeypatch, capsys) -> None:
    alm = Almanac(config)
    post, calls = scripted([
        tool_call("echo_tool", {"word": "hi"}),
        {"role": "assistant", "content": "Hello back."},
        {"role": "assistant", "content": "Still here."},
    ])
    from almanac import chat

    real = chat.Agent
    monkeypatch.setattr(chat, "Agent", lambda *a, **k: real(*a, post=post, **k))
    assert run_ask(alm, "say hi", None, False, False, False, thread_id="game", stream_json=True) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "thread" and events[0]["id"] == "game" and events[0]["new"] is True
    assert "tool" in kinds and "result" in kinds
    assert events[-1] == {"type": "done", "answer": "Hello back.", "thread": "game", "title": "say hi"}
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Hello back."

    assert run_ask(alm, "still there?", None, False, False, False, thread_id="game", stream_json=True) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["new"] is False and events[0]["turns"] == 1
    assert calls[-1]["messages"][1]["content"] == "say hi"

    assert run_threads(alm, "list", None, True, 10) == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert listed[0]["type"] == "thread_info" and listed[0]["id"] == "game" and listed[0]["turns"] == 2
    assert listed[-1] == {"type": "end"}

    assert run_threads(alm, "show", "game", True, 10) == 0
    shown = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    msgs = [(e["role"], e["text"]) for e in shown if e["type"] == "message"]
    assert msgs == [("user", "say hi"), ("assistant", "Hello back."), ("user", "still there?"), ("assistant", "Still here.")]
    assert any(e["type"] == "tool" and e["name"] == "echo_tool" for e in shown)

    assert run_threads(alm, "rm", "game", False, 10) == 0
    assert run_threads(alm, "show", "game", True, 10) == 2
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["code"] == "thread"


def test_stream_json_bad_thread_and_failures(config, monkeypatch, capsys) -> None:
    alm = Almanac(config)
    assert run_ask(alm, "x", None, False, False, False, thread_id="../etc", stream_json=True) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "thread"

    from almanac import chat

    def boom(payload, on_text=None):
        raise RuntimeError("backend down")

    real = chat.Agent
    monkeypatch.setattr(chat, "Agent", lambda *a, **k: real(*a, post=boom, **k))
    assert run_ask(alm, "x", None, False, False, False, new_thread=True, stream_json=True) == 1
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert last["type"] == "error" and "backend down" in last["message"]


def test_stream_json_never_approves_changes(config, tmp_path, monkeypatch, capsys) -> None:
    alm = Almanac(config)
    post, _ = scripted([tool_call("touch_tool", {"name": "made"}), {"role": "assistant", "content": "ok"}])
    from almanac import chat

    real = chat.Agent
    monkeypatch.setattr(chat, "Agent", lambda *a, **k: real(*a, post=post, **k))
    assert run_ask(alm, "make it", None, True, False, False, stream_json=True) == 0
    assert not (tmp_path / "made").exists()
    result = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"result"' in line][0]
    assert result["summary"].startswith("NOT RUN")


def test_json_lines_are_ascii() -> None:
    buf = io.StringIO()
    JsonLines(buf).emit("text", text="café ✓")
    line = buf.getvalue()
    assert line.isascii() and line.endswith("\n") and json.loads(line)["text"] == "café ✓"


def test_thread_file_round_trip(config) -> None:
    store = ThreadStore(config.state_dir)
    t = Thread(id="rt")
    t.add_turn([{"role": "user", "content": "  a   long\nquestion "}, {"role": "assistant", "content": "yes", "extra": 1}], "qwen")
    store.save(t)
    back = store.load("rt")
    assert back.title == "a long question" and back.model == "qwen"
    assert back.messages[1] == {"role": "assistant", "content": "yes"}
    assert oct((store.dir / "rt.json").stat().st_mode & 0o777) == "0o600"
