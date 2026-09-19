"""Autopilot tools offered through almanac's MCP server (and the local agent).

* ``autopilot_status`` (read): state, today's spend, what waits on approval or the owner.
* ``autopilot_add`` (change): queue a task. Confirmation required, like every change tool.
* ``autopilot_pause`` (change): pause or resume. Confirmation required.

They only touch the queue database; they never start the loop, run code or act in game.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import Config

CONFIRM = {"type": "string", "description": "Token from the plan, after the human approved it."}

TOOLS: dict[str, dict[str, Any]] = {
    "autopilot_status": {
        "safety": "read",
        "description": "autopilot status: running/paused, task counts, today's coding spend vs caps, items waiting on game approval or on the owner.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "autopilot_add": {
        "safety": "change",
        "description": "Queue a task for autopilot (optionally for one configured repo). The first call returns a plan and a confirm token.",
        "schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to do, one or two sentences."},
                "repo": {"type": "string", "description": "A repo name from the autopilot allow-list (optional)."},
                "priority": {"type": "number", "minimum": 0, "maximum": 100},
                "confirm": CONFIRM,
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    "autopilot_pause": {
        "safety": "change",
        "description": "Pause (paused=true) or resume (paused=false) autopilot. The first call returns a plan and a confirm token.",
        "schema": {
            "type": "object",
            "properties": {"paused": {"type": "boolean"}, "confirm": CONFIRM},
            "required": ["paused"],
            "additionalProperties": False,
        },
    },
}


class AutopilotToolError(ValueError):
    pass


def validate(name: str, args: dict[str, Any], config: Config) -> dict[str, Any]:
    from .settings import Settings

    if name == "autopilot_add":
        text = str(args.get("text", "")).strip()
        if not text or len(text) > 2000:
            raise AutopilotToolError("text is required (at most 2000 characters)")
        repo = str(args.get("repo", "") or "")
        if repo and repo not in Settings.from_config(config).repos:
            raise AutopilotToolError(f"unknown repo {repo!r}")
        return {"text": text, "repo": repo, "priority": args.get("priority")}
    if name == "autopilot_pause":
        if not isinstance(args.get("paused"), bool):
            raise AutopilotToolError("paused must be true or false")
        return {"paused": args["paused"]}
    return {}


def plan(name: str, args: dict[str, Any], config: Config) -> str:
    clean = validate(name, args, config)
    if name == "autopilot_add":
        return f"Queue an autopilot task{' for ' + clean['repo'] if clean['repo'] else ''}: {clean['text']}"
    return "Pause autopilot (no new steps start)" if clean["paused"] else "Resume autopilot"


def run(name: str, args: dict[str, Any], config: Config) -> str:
    from . import app

    settings, store = app.open_store(config)
    try:
        if name == "autopilot_status":
            return json.dumps(app.status(settings, store), indent=1)
        clean = validate(name, args, config)
        if name == "autopilot_add":
            task_id, _ = app.add(store, clean["text"], clean["repo"], clean["priority"])
            return f"queued autopilot task #{task_id}"
        app.pause(store, clean["paused"])
        return "autopilot paused" if clean["paused"] else "autopilot resumed"
    finally:
        store.close()
