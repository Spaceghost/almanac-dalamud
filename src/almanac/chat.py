"""``almanac chat``: a small terminal REPL for the local agent.

Made to run in narrow terminals, including a Ghostty tab inside FFXIV: plain
streamed text, one dim line per tool call, readline history, a few slash
commands. Progress also shows on companion status boards (XivMcp).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from typing import Any

from .agent import Agent, GuardRefused, Transcript, save_run, tty_approver
from .service import Almanac
from .threads import Thread, ThreadError, ThreadStore, trim_history

HELP = """/help              this help
/tools             tools the model can use now
/actions on|off    allow game action/chat tools (the game still asks you to confirm)
/change on|off     allow almanac change tools (each asks y/N here)
/clear             forget the conversation (a saved chat starts a new thread)
/thread            which thread this chat is saved in
/quit              leave (Ctrl-D works too)"""


class Style:
    def __init__(self, stream: Any = sys.stdout) -> None:
        self.on = stream.isatty() and not os.environ.get("NO_COLOR")

    def dim(self, text: str) -> str:
        return f"\033[2m{text}\033[0m" if self.on else text

    def bold(self, text: str) -> str:
        return f"\033[1m{text}\033[0m" if self.on else text


def _width() -> int:
    return max(40, min(120, shutil.get_terminal_size((100, 24)).columns))


def _history(almanac: Almanac) -> None:
    try:
        import readline
    except ImportError:
        return
    path = almanac.config.state_dir / "chat_history"
    try:
        readline.read_history_file(path)
    except OSError:
        pass
    readline.set_history_length(1000)
    import atexit

    atexit.register(lambda: readline.write_history_file(path))


def one_line(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def make_printers(style: Style) -> tuple[Any, Any]:
    def on_text(chunk: str) -> None:
        sys.stdout.write(chunk)
        sys.stdout.flush()

    def on_event(kind: str, data: dict[str, Any]) -> None:
        width = _width()
        if kind == "tool":
            print(style.dim(one_line(f"  → {data['name']} {one_line(data['args'], width)}", width - 2)), flush=True)
        elif kind == "result":
            first = data["text"].splitlines()[0] if data["text"].strip() else "(no output)"
            lines = data["text"].count("\n") + 1
            print(style.dim(one_line(f"  ← {first}  [{lines} lines]", width - 2)), flush=True)

    return on_text, on_event


class JsonLines:
    """``--stream-json``: one JSON object per line on stdout, for programs.

    Events: ``thread`` {id, title, new}, ``note`` {text}, ``text`` {text} (a
    piece of the answer as it streams), ``tool`` {name, args}, ``result``
    {name, summary, lines}, ``done`` {answer, thread}, ``error`` {message,
    code}. ASCII only (``ensure_ascii``), so a terminal or a pipe in between
    cannot split a character; every line is flushed at once.
    """

    def __init__(self, stream: Any = None) -> None:
        self.stream = stream or sys.stdout

    def emit(self, kind: str, **fields: Any) -> None:
        self.stream.write(json.dumps({"type": kind, **fields}, ensure_ascii=True) + "\n")
        self.stream.flush()

    def sinks(self) -> tuple[Any, Any]:
        def on_text(chunk: str) -> None:
            self.emit("text", text=chunk)

        def on_event(kind: str, data: dict[str, Any]) -> None:
            if kind == "tool":
                self.emit("tool", name=data["name"], args=data["args"])
            elif kind == "result":
                text = data["text"]
                first = text.strip().splitlines()[0] if text.strip() else "(no output)"
                self.emit("result", name=data["name"], summary=one_line(first, 200), lines=text.count("\n") + 1)

        return on_text, on_event


def thread_turn(agent: Agent, store: ThreadStore | None, thread: Thread, text: str,
                on_text: Any = None, on_event: Any = None) -> str:
    """One follow-up in `thread`: its earlier turns (trimmed to the model's
    context) go to the model before `text`; the new turn is appended and, with
    a store, saved."""
    agent.resume(trim_history(thread.messages, agent.history_budget()))
    base = len(agent.messages)
    answer = agent.turn(text, on_text, on_event)
    thread.add_turn(agent.messages[base:], agent.model)
    if store is not None:
        store.save(thread)
    return answer


def pick_thread(store: ThreadStore, thread_id: str | None, continue_last: bool, new: bool) -> tuple[Thread | None, bool]:
    """The thread the options name, and whether it is new. (None, False) without any."""
    if thread_id:
        fresh = not store.exists(thread_id)
        return store.open(thread_id), fresh
    if continue_last:
        latest = store.latest()
        return (latest, False) if latest else (store.open(None), True)
    if new:
        return store.open(None), True
    return None, False


def run_chat(almanac: Almanac, model: str | None = None, allow_change: bool = False, allow_game_actions: bool = False,
             thread_id: str | None = None, continue_last: bool = False, new_thread: bool = False) -> int:
    style = Style()
    _history(almanac)
    store = ThreadStore(almanac.config.state_dir)
    thread, _ = pick_thread(store, thread_id, continue_last, new_thread)
    persist = thread is not None
    if thread is None:
        thread = Thread(id="")

    def build(change: bool, actions: bool) -> Agent:
        agent = Agent(almanac, model=model, allow_change=change, allow_game_actions=actions)
        agent.start()
        return agent

    agent = build(allow_change, allow_game_actions)
    saved = f" · thread {thread.id} ({thread.turns()} turns)" if persist else ""
    print(style.bold("almanac chat") + style.dim(f"  model {agent.model}{saved} · /help · Ctrl-D to quit"))
    for note in agent.companions.notes:
        print(style.dim("  " + note))
    on_text, on_event = make_printers(style)
    while True:
        try:
            line = input(style.bold("› ") if style.on else "> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.startswith("/"):
            cmd, _, arg = line.partition(" ")
            if cmd in ("/quit", "/exit", "/q"):
                return 0
            if cmd == "/help":
                print(HELP)
            elif cmd == "/tools":
                for fn in agent.functions():
                    print(one_line(f"{fn['function']['name']:28} {fn['function']['description']}", _width()))
            elif cmd == "/clear":
                thread = store.open(None) if persist else Thread(id="")
                print(style.dim("  conversation cleared" + (f"; new thread {thread.id}" if persist else "")))
            elif cmd == "/thread":
                print(style.dim(f"  thread {thread.id} · {thread.turns()} turns · {thread.title}" if persist else "  not saved (start with --thread ID, --continue or --new-thread)"))
            elif cmd in ("/actions", "/change") and arg in ("on", "off"):
                allow_game_actions = arg == "on" if cmd == "/actions" else allow_game_actions
                allow_change = arg == "on" if cmd == "/change" else allow_change
                agent = build(allow_change, allow_game_actions)
                print(style.dim(f"  game actions {'on' if allow_game_actions else 'off'}, change tools {'on' if allow_change else 'off'}"))
            else:
                print(style.dim("  unknown command; /help"))
            continue
        agent.companions.post_status(f"working: {line[:150]}", "running")
        try:
            answer = thread_turn(agent, store if persist else None, thread, line, on_text, on_event)
            print(flush=True)
            agent.companions.post_status(f"done: {line[:150]}", "done", progress=1.0, detail=answer)
        except GuardRefused as exc:
            print(style.dim(f"  {exc}"))
            agent.companions.post_status("waiting: memory is tight", "failed", detail=str(exc))
        except KeyboardInterrupt:
            print(style.dim("\n  interrupted"))
            agent.companions.post_status("interrupted", "info")
        except Exception as exc:  # keep the REPL alive
            print(style.dim(f"  error: {exc}"))
            agent.companions.post_status("failed", "failed", detail=str(exc))


def run_ask(almanac: Almanac, task: str, model: str | None, allow_change: bool, allow_game_actions: bool, verbose: bool,
            thread_id: str | None = None, continue_last: bool = False, new_thread: bool = False,
            stream_json: bool = False) -> int:
    """``almanac ask``: one question; with a thread option, a follow-up in that thread."""
    out = JsonLines() if stream_json else None
    store = ThreadStore(almanac.config.state_dir)
    try:
        thread, fresh = pick_thread(store, thread_id, continue_last, new_thread)
    except ThreadError as exc:
        if out:
            out.emit("error", message=str(exc), code="thread")
        else:
            print(str(exc), file=sys.stderr)
        return 2
    style = Style()
    if out:
        on_text, on_event = out.sinks()
    else:
        on_text, on_event = make_printers(style)
        if not (verbose or sys.stdout.isatty()):
            on_event = None
    # a program reading --stream-json cannot answer y/N: change tools stay unapproved
    approver = (lambda name, plan: False) if out else tty_approver
    agent = Agent(almanac, model=model, allow_change=allow_change, allow_game_actions=allow_game_actions, approver=approver)
    if out and thread is not None:
        out.emit("thread", id=thread.id, title=thread.title, new=fresh, turns=thread.turns())
    for note in agent.companions.notes:
        if out:
            out.emit("note", text=note)
        else:
            print(style.dim("  " + note), file=sys.stderr)
    try:
        if thread is None:
            transcript = agent.run(task, on_text=on_text, on_event=on_event)
            answer = transcript.answer
        else:
            transcript = Transcript(task)
            agent.companions.post_status(f"started: {task[:150]}", "running", progress=0.0)
            answer = thread_turn(agent, store, thread, task, on_text, on_event)
            transcript.answer = answer
            agent.companions.post_status(f"done: {task[:140]}", "done", progress=1.0, detail=answer)
    except GuardRefused as exc:
        if out:
            out.emit("error", message=str(exc), code="guard")
        else:
            print(str(exc), file=sys.stderr)
        return 75
    except Exception as exc:
        if not out:
            raise
        out.emit("error", message=f"{exc.__class__.__name__}: {exc}", code="failed")
        return 1
    path = save_run(almanac, "ask", transcript)
    if out:
        out.emit("done", answer=answer, thread=thread.id if thread else None, title=thread.title if thread else "")
    else:
        print(flush=True)
        if thread is not None:
            print(style.dim(f"(thread: {thread.id}; follow up with almanac ask --thread {thread.id} ...)"), file=sys.stderr)
        print(style.dim(f"(transcript: {path})"), file=sys.stderr)
    return 0


def run_threads(almanac: Almanac, action: str, thread_id: str | None, as_json: bool, limit: int) -> int:
    """``almanac threads [list|show ID|rm ID]``."""
    store = ThreadStore(almanac.config.state_dir)
    out = JsonLines() if as_json else None
    try:
        if action == "list":
            for thread in store.list()[:limit]:
                if out:
                    out.emit("thread_info", **thread.summary())
                else:
                    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(thread.updated))
                    print(f"{thread.id:24} {stamp}  {thread.turns():3} turns  {thread.title}")
            if out:
                out.emit("end")
            return 0
        if not thread_id:
            raise ThreadError(f"usage: almanac threads {action} ID")
        if action == "rm":
            if not store.delete(thread_id):
                raise ThreadError(f"no thread {thread_id}")
            if out:
                out.emit("deleted", id=thread_id)
            else:
                print(f"deleted {thread_id}")
            return 0
        thread = store.load(thread_id)
        if out:
            out.emit("thread", id=thread.id, title=thread.title, new=False, turns=thread.turns())
        for message in thread.messages:
            role, content = message.get("role"), str(message.get("content") or "")
            if role == "tool":
                continue
            if role == "assistant" and message.get("tool_calls"):
                for call in message["tool_calls"]:
                    fn = call.get("function", {})
                    if out:
                        try:
                            args = json.loads(fn.get("arguments") or "{}")
                        except ValueError:
                            args = {}
                        out.emit("tool", name=fn.get("name", ""), args=args)
                    else:
                        print(f"  → {fn.get('name', '')}")
                if not content.strip():
                    continue
            if out:
                out.emit("message", role=role, text=content)
            else:
                print(f"{'you' if role == 'user' else 'almanac'}: {content}\n")
        if out:
            out.emit("end")
        return 0
    except ThreadError as exc:
        if out:
            out.emit("error", message=str(exc), code="thread")
        else:
            print(str(exc), file=sys.stderr)
        return 2
