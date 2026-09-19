"""Owner approval for anything autopilot would land outside its own sandbox.

Coding sessions run in throwaway git worktrees, so their edits are contained.
Two things leave that sandbox, and both are gated here:

* ``code`` - a task's first coding session (one approval per task, so a task the
  owner said yes to can then work all night on its own steps);
* ``push`` - every push of a task branch and the draft PR that follows it.

Game actions are gated by XivMcp itself (``game.request_action`` returns its own
ticket); this module mirrors that shape so the loop treats both the same way.

Three properties the owner asked for:

* **it piles up** - a request becomes a row in the queue database, so it
  survives a restart, a reboot, or the owner being asleep;
* **it resumes** - the step that asked parks on the ticket and continues exactly
  where it stopped once the answer arrives (``poll_tickets`` in the runner);
* **allow sessions** - ``almanac autopilot allow --minutes 5`` is sudo-like: for
  that window, matching requests are approved as they arrive (and anything
  already pending in scope is approved at once). The window is stored with an
  expiry; nothing extends it but the owner asking again.

Denial is never overridden and never retried automatically: the task goes to
"needs you" with the reason.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .store import Store

# Gate kinds. "game_action" is listed so the policy can name it, but it is
# enforced by XivMcp's own ticket flow, not here.
KINDS = ("code", "push", "game_action")

# Which allow-session scope covers which kind. "all" covers everything.
SCOPES = {"code": ("all", "code"), "push": ("all", "push"), "game_action": ("all", "game")}

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"


@dataclass
class Approval:
    """Same surface as ``game.Ticket`` so the runner can park on either."""

    ticket: str
    kind: str
    state: str
    title: str = ""
    detail: str = ""
    actor: str = ""
    result: str = ""
    task_id: int = 0
    step_id: int = 0
    created: float = 0.0

    @property
    def id(self) -> str:
        return self.ticket

    @property
    def settled(self) -> bool:
        return self.state in (APPROVED, DENIED)

    @property
    def approved(self) -> bool:
        return self.state == APPROVED


def is_local(ticket_id: str) -> bool:
    """True for approvals owned by this module (as opposed to an XivMcp ticket)."""
    return ticket_id.startswith("ap-")


class Approvals:
    def __init__(
        self,
        store: Store,
        policy: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
        log: Callable[[int | None, str, str], None] | None = None,
    ) -> None:
        self.store = store
        self.policy = dict(policy or {})
        self.clock = clock
        self._log = log or (lambda task_id, kind, message: store.log(task_id, kind, message))

    # -- policy ---------------------------------------------------------------
    @property
    def required(self) -> set[str]:
        return {str(k) for k in self.policy.get("require", KINDS)}

    def needed(self, kind: str) -> bool:
        return kind in self.required

    @property
    def allow_minutes(self) -> float:
        return float(self.policy.get("allow_session_minutes", 5))

    @property
    def max_allow_minutes(self) -> float:
        return float(self.policy.get("max_allow_session_minutes", 60))

    def code_scope(self, task_id: int, step_id: int) -> str:
        """One approval per task (default) or per coding step."""
        per_step = str(self.policy.get("code_scope", "task")) == "step"
        return f"code:{task_id}:{step_id}" if per_step else f"code:{task_id}"

    # -- allow sessions -------------------------------------------------------
    def open_allow(self, scope: str = "all", minutes: float | None = None, actor: str = "owner") -> tuple[float, list[Approval]]:
        """Open an allow session. Returns (expiry, approvals settled right away)."""
        span = min(self.max_allow_minutes, float(self.allow_minutes if minutes is None else minutes))
        until = self.clock() + span * 60
        self.store.set_flag(f"allow:{scope}", str(until))
        self._log(None, "allow", f"allow session for {scope} open for {span:g} min (until {_hhmm(until)}), opened by {actor}")
        settled = [self.settle(a.ticket, APPROVED, actor, f"allow session ({scope})") for a in self.pending() if self._in_scope(a.kind, scope)]
        return until, settled

    def close_allow(self, scope: str = "all") -> None:
        self.store.set_flag(f"allow:{scope}", "")
        self._log(None, "allow", f"allow session for {scope} closed")

    def allow_until(self, kind: str) -> float:
        """Latest expiry of an allow session covering ``kind`` (0 = none active)."""
        now = self.clock()
        best = 0.0
        for scope in SCOPES.get(kind, ("all",)):
            until = float(self.store.flag(f"allow:{scope}", "0") or 0)
            if until > now:
                best = max(best, until)
        return best

    def sessions(self) -> dict[str, float]:
        now = self.clock()
        out = {}
        for scope in ("all", "code", "push", "game"):
            until = float(self.store.flag(f"allow:{scope}", "0") or 0)
            if until > now:
                out[scope] = until
        return out

    def _in_scope(self, kind: str, scope: str) -> bool:
        return scope == "all" or scope in SCOPES.get(kind, ())

    # -- requests -------------------------------------------------------------
    def request(self, kind: str, task_id: int, step_id: int, title: str, detail: str = "", scope_key: str = "") -> Approval:
        """Ask for approval. An allow session in scope approves it immediately.

        A ``scope_key`` already answered is reused, so one yes covers the whole
        task (see ``code_scope``) instead of asking again at every step.
        """
        if scope_key:
            previous = self.by_scope(scope_key)
            if previous is not None and previous.settled:
                return previous
            if previous is not None:
                return previous  # still pending: park on the same ticket
        row = self.store.add_approval(kind, task_id, step_id, title[:300], detail[:8000], scope_key)
        approval = _approval(row)
        until = self.allow_until(kind)
        if until:
            approval = self.settle(approval.ticket, APPROVED, "allow-session", f"allow session open until {_hhmm(until)}")
            self._log(task_id or None, "approval", f"{approval.ticket} {kind} auto-approved by the open allow session: {title[:150]}")
        else:
            self._log(task_id or None, "approval", f"{approval.ticket} {kind} needs you: {title[:150]}")
        return approval

    def get(self, ticket: str) -> Approval:
        row = self.store.approval(ticket)
        if row is None:
            raise KeyError(f"no approval {ticket}")
        return _approval(row)

    def by_scope(self, scope_key: str) -> Approval | None:
        row = self.store.approval_by_scope(scope_key)
        return _approval(row) if row else None

    def pending(self) -> list[Approval]:
        return [_approval(r) for r in self.store.approvals(PENDING)]

    def recent(self, limit: int = 20) -> list[Approval]:
        return [_approval(r) for r in self.store.approvals(None, limit)]

    # -- answers --------------------------------------------------------------
    def settle(self, ticket: str, state: str, actor: str = "owner", result: str = "") -> Approval:
        self.store.settle_approval(ticket, state, actor, result)
        approval = self.get(ticket)
        self._log(approval.task_id or None, "approval", f"{ticket} {approval.kind} {state} by {actor}{': ' + result if result else ''}")
        return approval

    def approve(self, ticket: str, actor: str = "owner", result: str = "") -> Approval:
        return self.settle(ticket, APPROVED, actor, result)

    def deny(self, ticket: str, actor: str = "owner", reason: str = "") -> Approval:
        return self.settle(ticket, DENIED, actor, reason)

    def answer_all(self, state: str, actor: str = "owner", reason: str = "", kind: str = "") -> list[Approval]:
        return [self.settle(a.ticket, state, actor, reason) for a in self.pending() if not kind or a.kind == kind]


def _approval(row: dict[str, Any]) -> Approval:
    return Approval(
        ticket=str(row["ticket"]), kind=str(row["kind"]), state=str(row["state"]), title=str(row["title"]),
        detail=str(row["detail"]), actor=str(row["actor"]), result=str(row["result"]), task_id=int(row["task_id"] or 0),
        step_id=int(row["step_id"] or 0), created=float(row["created"]),
    )


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))
