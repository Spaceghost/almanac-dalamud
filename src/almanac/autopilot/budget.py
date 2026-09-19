"""Caps for cloud coding sessions: per run (turns, wall time) and per day (runs, tokens, cost).

The day is the local calendar day. When any daily cap is reached, coding
steps wait until the next day; everything local (planning, tests, triage,
docs, game reads) keeps going.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Any

from .store import Store


@dataclass
class Verdict:
    ok: bool
    reason: str


def local_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def next_local_midnight(ts: float) -> float:
    day = dt.datetime.fromtimestamp(ts).date() + dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time(0, 0, 5)).timestamp()


class Budget:
    def __init__(self, store: Store, caps: dict[str, Any], clock: Any = time.time) -> None:
        self.store = store
        self.caps = caps
        self.clock = clock

    @property
    def today(self) -> str:
        return local_day(self.clock())

    def spent(self) -> dict[str, float]:
        return self.store.usage_for(self.today)

    def may_code(self) -> Verdict:
        used = self.spent()
        runs, tokens, cost = int(self.caps["coding_runs_per_day"]), int(self.caps["tokens_per_day"]), float(self.caps["cost_usd_per_day"])
        if used["runs"] >= runs:
            return Verdict(False, f"daily coding-run cap reached ({used['runs']}/{runs})")
        if used["tokens"] >= tokens:
            return Verdict(False, f"daily token cap reached ({used['tokens']}/{tokens})")
        if used["cost"] >= cost:
            return Verdict(False, f"daily cost cap reached (${used['cost']:.2f}/${cost:.2f})")
        return Verdict(True, f"{runs - used['runs']} coding runs left today")

    def may_code_local(self) -> Verdict:
        used = self.store.usage_for(self.today, "local")["runs"]
        cap = int(self.caps.get("local_runs_per_day", 40))
        if used >= cap:
            return Verdict(False, f"daily local coding-run cap reached ({used}/{cap})")
        return Verdict(True, f"{cap - used} local coding runs left today")

    def record(self, task_id: int, coder: str, tokens: int, cost: float, seconds: float) -> None:
        self.store.record_usage(self.today, task_id, coder, tokens, cost, seconds)

    def resume_at(self) -> float:
        return next_local_midnight(self.clock())

    def per_run(self) -> dict[str, int]:
        return {
            "max_turns": int(self.caps["max_turns"]),
            "max_seconds": int(float(self.caps["max_wall_minutes"]) * 60),
            "local_max_seconds": int(float(self.caps.get("local_max_wall_minutes", 30)) * 60),
        }


def backoff_seconds(attempts: int, base: float, maximum: float) -> float:
    """Exponential backoff: base, 2*base, 4*base ... capped at maximum."""
    return float(min(maximum, base * (2 ** max(0, attempts - 1))))
