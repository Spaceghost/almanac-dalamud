"""``almanac autopilot <command>``: run the loop and control it."""

from __future__ import annotations

import argparse
import json
import sys

from ..config import Config
from ..service import Almanac
from . import app


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("autopilot", help="autonomous coding + game companion loop (see README)")
    p.set_defaults(func=main)
    cmds = p.add_subparsers(dest="ap_command", required=True)
    run = cmds.add_parser("run", help="run the loop in the foreground")
    run.add_argument("--once", action="store_true", help="one tick, then exit")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_const", const=True, help="plan, test and log; no coding sessions, pushes, PRs or tickets")
    mode.add_argument("--live", dest="dry_run", action="store_const", const=False, help="override dry_run = true in the config")
    cmds.add_parser("status", help="state, today's spend, what waits on you").add_argument("--json", action="store_true")
    cmds.add_parser("pause", help="stop starting new steps (tickets and sources are still polled)")
    cmds.add_parser("resume", help="undo pause")
    cmds.add_parser("stop", help="ask the running loop to exit after the current step").add_argument(
        "--now", action="store_true", help="create the kill switch file: running sessions are killed within a second"
    )
    a = cmds.add_parser("add", help='queue a task: almanac autopilot add "fix X" --repo NAME')
    a.add_argument("text", nargs="+")
    a.add_argument("--repo", default="")
    a.add_argument("--priority", type=float)
    lst = cmds.add_parser("list", help="open tasks by value")
    lst.add_argument("--all", action="store_true")
    cmds.add_parser("log", help="one task's plan and log").add_argument("task", type=int)
    cmds.add_parser("retry", help="put a task that needs you back in the queue").add_argument("task", type=int)
    cmds.add_parser("cancel", help="cancel a task").add_argument("task", type=int)
    cmds.add_parser("digest", help="write the digest now and print its path")
    cmds.add_parser("pool", help="model backends: health, roles, busy slots")


def main(cfg: Config, ns: argparse.Namespace) -> int:
    settings, store = app.open_store(cfg)
    cmd = ns.ap_command
    if cmd == "run":
        pilot = app.build(Almanac(cfg), dry_run=ns.dry_run)
        return pilot.run_forever(once=ns.once)
    if cmd == "status":
        print(json.dumps(app.status(settings, store), indent=1) if ns.json else app.status_text(settings, store))
    elif cmd in ("pause", "resume"):
        app.pause(store, cmd == "pause")
        print(f"autopilot {cmd}d")
    elif cmd == "stop":
        app.stop(store)
        if ns.now:
            settings.kill_switch.parent.mkdir(parents=True, exist_ok=True)
            settings.kill_switch.write_text("stopped with `almanac autopilot stop --now`; delete this file to allow runs again\n")
            print(f"kill switch set: {settings.kill_switch} (delete it to run again)")
        else:
            print("stop requested; the loop exits after its current step")
    elif cmd == "add":
        if ns.repo and ns.repo not in settings.repos:
            print(f"unknown repo {ns.repo!r}; configured: {', '.join(settings.repos) or 'none'}", file=sys.stderr)
            return 2
        task_id, _ = app.add(store, " ".join(ns.text), ns.repo, ns.priority)
        Almanac(cfg).audit("autopilot_add", "autopilot", {"task": task_id, "repo": ns.repo}, "cli")
        print(f"queued task #{task_id}")
    elif cmd == "list":
        print(app.list_text(store, ns.all))
    elif cmd == "log":
        print(app.log_text(store, ns.task))
    elif cmd == "retry":
        app.retry(store, ns.task)
        print(f"task #{ns.task} queued again")
    elif cmd == "cancel":
        tickets = app.cancel(store, ns.task)
        print(f"task #{ns.task} cancelled" + (f"; open game tickets: {', '.join(tickets)}" if tickets else ""))
    elif cmd == "pool":
        print(app.pool_text(app.make_pool(cfg, settings)))
    elif cmd == "digest":
        print(app.build(Almanac(cfg)).maybe_digest(force=True))
    return 0
