"""FFXIV through XivMcp: observe and use UI freely, act only through approval tickets.

What autopilot does in game:

* read state and use UI-tier tools (agent board, toasts, map flags, tracker
  objectives): these are XivMcp's ``read`` and ``ui`` tiers;
* post progress to the agent board (``post_status``);
* show "needs you" items and pending approvals as quest-tracker objectives,
  if the server lists an objective tool (feature-detected from tools/list);
* **request** game actions with ``request_action``: XivMcp returns a ticket
  (state ``pending``) and the player approves or denies it in game, now or
  later (optionally inside a short allow session the player opens). Autopilot
  parks the plan step on that ticket, keeps working on other tasks, polls
  ``get_ticket`` and resumes the step when it is approved, or re-plans when it
  is denied, expires or is cancelled.

What it never does: call an action/chat-tier tool directly, bypass or
auto-answer an approval, or automate gameplay (movement, combat, gathering,
crafting, trading, market board, chat). Automating play breaks the game's
terms of service; such tools are refused here even if a plan asks for them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..upstream import Upstream, UpstreamError

# Never requested, whatever a plan says: gameplay automation is off limits.
FORBIDDEN_WORDS = ("move", "walk", "mount", "fly", "teleport", "combat", "attack", "cast", "target", "gather", "craft",
                   "trade", "market", "retainer_sell", "buy", "sell", "chat", "say", "tell", "duty_queue", "loot", "emote")

TICKET_DONE = {"approved", "completed", "succeeded", "done", "executed"}
TICKET_DENIED = {"denied", "rejected", "declined", "expired", "cancelled", "canceled", "failed", "error"}


class GameError(RuntimeError):
    pass


@dataclass
class Ticket:
    id: str
    state: str
    result: str = ""
    raw: dict[str, Any] | None = None

    @property
    def settled(self) -> bool:
        return self.state in TICKET_DONE or self.state in TICKET_DENIED

    @property
    def approved(self) -> bool:
        return self.state in TICKET_DONE


class Game(Protocol):
    def refresh(self, force: bool = False) -> bool: ...
    def read_tools(self) -> set[str]: ...
    def action_tools(self) -> set[str]: ...
    def post_status(self, status: str, state: str = "running", progress: float | None = None, detail: str | None = None) -> None: ...
    def toast(self, message: str) -> bool: ...
    def set_objectives(self, items: list[str]) -> bool: ...
    def read(self, tool: str, args: dict[str, Any]) -> str: ...
    def request_action(self, tool: str, args: dict[str, Any], reason: str, resume_token: str) -> Ticket: ...
    def get_ticket(self, ticket_id: str) -> Ticket: ...
    def cancel_ticket(self, ticket_id: str) -> None: ...


def is_forbidden(tool: str) -> bool:
    name = tool.lower()
    return any(word in name.split("_") or name.startswith(word) for word in FORBIDDEN_WORDS)


def _json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0:
            return {"text": text}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {"text": text}
    return data if isinstance(data, dict) else {"value": data}


def ticket_from(data: dict[str, Any]) -> Ticket:
    inner = data["ticket"] if isinstance(data.get("ticket"), dict) else data
    ticket_id = str(inner.get("ticket_id") or inner.get("id") or data.get("ticket_id") or "")
    state = str(inner.get("state") or inner.get("status") or "unknown").lower()
    result = inner.get("result")
    return Ticket(ticket_id, state, json.dumps(result)[:4000] if result is not None else "", data)


class XivMcpGame:
    """The real link, over almanac's ``Upstream`` client. Every failure is soft: the game may be closed."""

    def __init__(self, upstream: Upstream, settings: dict[str, Any], audit: Any = None, clock: Any = time.time) -> None:
        self.up = upstream
        self.settings = settings
        self.audit = audit or (lambda *a, **k: None)
        self.clock = clock
        self.tools: dict[str, Any] = {}
        self.online = False
        self._checked = 0.0

    def refresh(self, force: bool = False) -> bool:
        if not force and self.clock() - self._checked < 60:
            return self.online
        self._checked = self.clock()
        try:
            self.tools = {t.name: t for t in self.up.list_tools()}
            self.online = True
        except UpstreamError:
            self.tools, self.online = {}, False
        return self.online

    def _tier(self, name: str) -> str:
        tool = self.tools.get(name)
        return tool.tier if tool else ""

    def read_tools(self) -> set[str]:
        wanted = set(self.settings.get("read_tools") or [])
        free = {n for n, t in self.tools.items() if t.tier in ("read",)}
        return (free & wanted) if wanted else free

    def action_tools(self) -> set[str]:
        if "request_action" not in self.tools:
            return set()
        return {n for n, t in self.tools.items() if t.tier == "action" and not is_forbidden(n)}

    def _call(self, name: str, args: dict[str, Any]) -> str:
        try:
            text, is_error = self.up.call(name, args)
        except UpstreamError as exc:
            self.online = False
            raise GameError(str(exc)) from exc
        if is_error:
            raise GameError(f"{name}: {text[:300]}")
        return text

    def post_status(self, status: str, state: str = "running", progress: float | None = None, detail: str | None = None) -> None:
        if not self.online or "post_status" not in self.tools:
            return
        args: dict[str, Any] = {"agent": self.settings.get("board_agent", "autopilot"), "status": status[:200], "state": state}
        if progress is not None:
            args["progress"] = max(0.0, min(1.0, progress))
        if detail:
            args["detail"] = detail[:2000]
        try:
            self._call("post_status", args)
        except (UpstreamError, GameError):
            self.online = False

    def toast(self, message: str) -> bool:
        tool = str(self.settings.get("toast_tool", "show_toast"))
        if not self.online or tool not in self.tools:
            return False
        try:
            self._call(tool, {"message": message[:200]})
            return True
        except (UpstreamError, GameError):
            return False

    def objective_tool(self) -> str:
        for name in self.settings.get("objective_tools", []):
            if name in self.tools:
                return str(name)
        return ""

    def set_objectives(self, items: list[str]) -> bool:
        tool = self.objective_tool()
        if not self.online or not tool:
            return False
        schema = self.tools[tool].input_schema.get("properties", {})
        key = next((k for k in ("objectives", "items", "lines") if k in schema), "objectives")
        args: dict[str, Any] = {key: [i[:120] for i in items[:8]]}
        if "agent" in schema:
            args["agent"] = self.settings.get("board_agent", "autopilot")
        try:
            self._call(tool, args)
            return True
        except (UpstreamError, GameError):
            return False

    def read(self, tool: str, args: dict[str, Any]) -> str:
        if self._tier(tool) not in ("read", "ui"):
            raise GameError(f"{tool} is not a read/ui tool on the game server")
        return self._call(tool, args)

    def request_action(self, tool: str, args: dict[str, Any], reason: str, resume_token: str) -> Ticket:
        if is_forbidden(tool):
            raise GameError(f"{tool} looks like gameplay automation; autopilot never requests it")
        if tool not in self.action_tools():
            raise GameError(f"{tool} is not an action tool offered for approval (or request_action is missing)")
        schema = self.tools["request_action"].input_schema.get("properties", {})
        key_args = next((k for k in ("arguments", "args", "params") if k in schema), "arguments")
        payload: dict[str, Any] = {"tool": tool, key_args: args}
        if "reason" in schema:
            payload["reason"] = reason
        if "resume_token" in schema:
            payload["resume_token"] = resume_token
        if "requester" in schema:
            payload["requester"] = self.settings.get("board_agent", "autopilot")
        ticket = ticket_from(_json(self._call("request_action", payload)))
        if not ticket.id:
            raise GameError("request_action returned no ticket id")
        self.audit("ticket", tool, args, "autopilot", ticket=ticket.id, reason=reason)
        return ticket

    def get_ticket(self, ticket_id: str) -> Ticket:
        ticket = ticket_from(_json(self._call("get_ticket", {"ticket_id": ticket_id})))
        return Ticket(ticket.id or ticket_id, ticket.state, ticket.result, ticket.raw)

    def cancel_ticket(self, ticket_id: str) -> None:
        if "cancel_ticket" in self.tools:
            self._call("cancel_ticket", {"ticket_id": ticket_id})


class NoGame:
    """Used when no game upstream is configured."""

    online = False

    def refresh(self, force: bool = False) -> bool:
        return False

    def read_tools(self) -> set[str]:
        return set()

    def action_tools(self) -> set[str]:
        return set()

    def post_status(self, *a: Any, **k: Any) -> None:
        return None

    def toast(self, message: str) -> bool:
        return False

    def set_objectives(self, items: list[str]) -> bool:
        return False

    def read(self, tool: str, args: dict[str, Any]) -> str:
        raise GameError("no game connection configured")

    def request_action(self, tool: str, args: dict[str, Any], reason: str, resume_token: str) -> Ticket:
        raise GameError("no game connection configured")

    def get_ticket(self, ticket_id: str) -> Ticket:
        raise GameError("no game connection configured")

    def cancel_ticket(self, ticket_id: str) -> None:
        return None
