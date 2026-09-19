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
from typing import Any

from .agent import Agent, GuardRefused, save_run
from .service import Almanac

HELP = """/help              this help
/tools             tools the model can use now
/actions on|off    allow game action/chat tools (the game still asks you to confirm)
/change on|off     allow almanac change tools (each asks y/N here)
/clear             forget the conversation
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


def run_chat(almanac: Almanac, model: str | None = None, allow_change: bool = False, allow_game_actions: bool = False) -> int:
    style = Style()
    _history(almanac)

    def build(change: bool, actions: bool) -> Agent:
        agent = Agent(almanac, model=model, allow_change=change, allow_game_actions=actions)
        agent.start()
        return agent

    agent = build(allow_change, allow_game_actions)
    print(style.bold("almanac chat") + style.dim(f"  model {agent.model} · /help · Ctrl-D to quit"))
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
                agent.start()
                print(style.dim("  conversation cleared"))
            elif cmd in ("/actions", "/change") and arg in ("on", "off"):
                allow_game_actions = arg == "on" if cmd == "/actions" else allow_game_actions
                allow_change = arg == "on" if cmd == "/change" else allow_change
                history = agent.messages[1:]
                agent = build(allow_change, allow_game_actions)
                agent.messages += history
                print(style.dim(f"  game actions {'on' if allow_game_actions else 'off'}, change tools {'on' if allow_change else 'off'}"))
            else:
                print(style.dim("  unknown command; /help"))
            continue
        agent.companions.post_status(f"working: {line[:150]}", "running")
        try:
            answer = agent.turn(line, on_text, on_event)
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


def run_ask(almanac: Almanac, task: str, model: str | None, allow_change: bool, allow_game_actions: bool, verbose: bool) -> int:
    style = Style()
    on_text, on_event = make_printers(style)
    agent = Agent(almanac, model=model, allow_change=allow_change, allow_game_actions=allow_game_actions)
    for note in agent.companions.notes:
        print(style.dim("  " + note), file=sys.stderr)
    try:
        transcript = agent.run(task, on_text=on_text, on_event=on_event if verbose or sys.stdout.isatty() else None)
    except GuardRefused as exc:
        print(str(exc), file=sys.stderr)
        return 75
    print(flush=True)
    path = save_run(almanac, "ask", transcript)
    print(style.dim(f"(transcript: {path})"), file=sys.stderr)
    return 0
