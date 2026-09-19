"""Autopilot tools offered through almanac's MCP server (and the local agent).

* ``autopilot_status`` (read): state, today's spend, what waits on approval or the owner.
* ``autopilot_add`` (change): queue a task. Confirmation required, like every change tool.
* ``autopilot_pause`` (change): pause or resume. Confirmation required.
* ``autopilot_pending`` (read): what waits for the owner's approval, with details.
* ``autopilot_approve`` (change): approve or deny a pending item, optionally opening a
  short allow session. Confirmation required, so the owner answers twice for a
  change that lands code: once here, once in the confirmation.

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
    "autopilot_pending": {
        "safety": "read",
        "description": "Approvals waiting for the owner: code changes, pushes and game actions autopilot has queued, oldest first, with what each would do.",
        "schema": {"type": "object", "properties": {"ticket": {"type": "string", "description": "One ticket id (ap-N) for its full detail."}}, "additionalProperties": False},
    },
    "autopilot_approve": {
        "safety": "change",
        "description": "Approve or deny a pending autopilot approval ('all' answers every pending one). Optionally open an allow session of N minutes, during which new requests are approved as they arrive.",
        "schema": {
            "type": "object",
            "properties": {
                "ticket": {"type": "string", "description": "Ticket id (ap-N) or 'all'."},
                "decision": {"type": "string", "enum": ["approve", "deny"]},
                "reason": {"type": "string"},
                "allow_minutes": {"type": "number", "minimum": 0, "maximum": 60, "description": "Open an allow session for this many minutes as well."},
                "confirm": CONFIRM,
            },
            "required": ["ticket", "decision"],
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
    if name == "autopilot_pending":
        return {"ticket": str(args.get("ticket", "") or "")}
    if name == "autopilot_approve":
        ticket = str(args.get("ticket", "")).strip()
        decision = str(args.get("decision", "")).strip()
        if not ticket:
            raise AutopilotToolError("ticket is required (a ticket id, or 'all')")
        if decision not in ("approve", "deny"):
            raise AutopilotToolError("decision must be 'approve' or 'deny'")
        minutes = float(args.get("allow_minutes") or 0)
        return {"ticket": ticket, "decision": decision, "reason": str(args.get("reason", "") or ""), "allow_minutes": minutes}
    if name == "autopilot_pause":
        if not isinstance(args.get("paused"), bool):
            raise AutopilotToolError("paused must be true or false")
        return {"paused": args["paused"]}
    return {}


def plan(name: str, args: dict[str, Any], config: Config) -> str:
    clean = validate(name, args, config)
    if name == "autopilot_add":
        return f"Queue an autopilot task{' for ' + clean['repo'] if clean['repo'] else ''}: {clean['text']}"
    if name == "autopilot_approve":
        what = "every pending approval" if clean["ticket"] in ("all", "*") else clean["ticket"]
        extra = f", and open a {clean['allow_minutes']:g}-minute allow session" if clean["allow_minutes"] else ""
        return f"{clean['decision'].capitalize()} {what}{extra}"
    return "Pause autopilot (no new steps start)" if clean["paused"] else "Resume autopilot"


def run(name: str, args: dict[str, Any], config: Config) -> str:
    from . import app

    settings, store = app.open_store(config)
    try:
        if name == "autopilot_status":
            return json.dumps(app.status(settings, store), indent=1)
        clean = validate(name, args, config)
        if name == "autopilot_pending":
            ticket = str(args.get("ticket", "") or "")
            return app.show_text(settings, store, ticket) if ticket else app.pending_text(settings, store)
        if name == "autopilot_add":
            task_id, _ = app.add(store, clean["text"], clean["repo"], clean["priority"], settings=settings)
            return f"queued autopilot task #{task_id}"
        if name == "autopilot_approve":
            state = "approved" if clean["decision"] == "approve" else "denied"
            answered = app.answer(settings, store, clean["ticket"], state, "owner (mcp)", clean["reason"])
            lines = [f"{a.ticket} {a.kind} {a.state} (task #{a.task_id})" for a in answered] or ["nothing was pending"]
            if clean["allow_minutes"]:
                until, extra = app.allow(settings, store, "all", clean["allow_minutes"], "owner (mcp)")
                lines.append(f"allow session until {__import__('time').strftime('%H:%M:%S', __import__('time').localtime(until))}"
                             + (f"; also approved {', '.join(a.ticket for a in extra)}" if extra else ""))
            return "\n".join(lines)
        app.pause(store, clean["paused"], settings)
        return "autopilot paused" if clean["paused"] else "autopilot resumed"
    finally:
        store.close()
