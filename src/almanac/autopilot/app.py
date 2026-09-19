"""Wiring (real implementations) and the control operations shared by the CLI and MCP."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ..config import Config
from ..service import Almanac
from ..upstream import Upstream
from .game import Game, NoGame, XivMcpGame
from .planner import Planner
from .pool import BackendModel, Pool, PoolModel
from .runner import Autopilot
from .sandbox import SubprocessRunner
from .settings import Settings
from .sources import SOURCE_VALUE, ReadOnlyGh
from .store import OPEN_STATES, Store


def open_store(config: Config) -> tuple[Settings, Store]:
    settings = Settings.from_config(config)
    return settings, Store(settings.db_path)


def build(almanac: Almanac, dry_run: bool | None = None) -> Autopilot:
    config = almanac.config
    settings, store = open_store(config)
    if dry_run is not None:
        settings.raw["dry_run"] = dry_run
    pool = make_pool(config, settings, store)
    guard = config.section("guard")
    planner = Planner(PoolModel(pool, "planner", guard), int(settings.caps["local_steps_per_task"]))
    game_cfg = settings.section("game")
    upstreams = dict(config.raw.get("upstreams", {}))
    game: Game = NoGame()
    if game_cfg.get("enabled") and game_cfg.get("upstream") in upstreams:
        up = Upstream.from_config(str(game_cfg["upstream"]), upstreams[str(game_cfg["upstream"])])
        up.categories = []  # autopilot filters by tier itself; it needs the meta/ticket tools
        game = XivMcpGame(up, game_cfg, audit=lambda e, n, a, c, **k: almanac.audit(e, n, a, c, **k))
    proc = SubprocessRunner(settings.kill_switch)
    home = Path.home()

    def gh_run(argv: list[str]) -> tuple[int, str]:
        result = proc(argv, home, dict(os.environ), 120)
        return result.code, result.output

    def get_json(url: str) -> Any:
        import httpx

        response = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "almanac-autopilot"})
        response.raise_for_status()
        return response.json()

    return Autopilot(
        settings, store, planner, game, proc, almanac.audit, pool=pool, gh=ReadOnlyGh(gh_run), get_json=get_json,
        backend_model=lambda b: BackendModel(b, guard),
    )


def make_pool(config: Config, settings: Settings, store: Store | None = None) -> Pool:
    gateway = dict(config.section("gateway"))
    if settings["planner_model"]:
        gateway["default_model"] = settings["planner_model"]
    on_event = (lambda m: store.log(None, "pool", m)) if store is not None else None
    return Pool.from_settings(settings.section("pool"), gateway, on_event=on_event)


# -- controls (no loop needed; they only touch the queue database) ------------

def add(store: Store, text: str, repo: str = "", priority: float | None = None, body: str = "") -> tuple[int, bool]:
    return store.add_task("manual", text, body, repo, SOURCE_VALUE["manual"] if priority is None else priority, f"manual:{time.time_ns()}")


def pause(store: Store, paused: bool) -> None:
    store.set_flag("paused", "1" if paused else "")
    store.log(None, "control", "paused" if paused else "resumed")


def stop(store: Store) -> None:
    store.set_flag("stop", "1")
    store.log(None, "control", "stop requested")


def status(settings: Settings, store: Store) -> dict[str, Any]:
    from .budget import Budget

    budget = Budget(store, settings.caps)
    counts: dict[str, int] = {}
    for task in store.tasks():
        counts[task.state] = counts.get(task.state, 0) + 1
    pid = store.flag("pid")
    return {
        "running": bool(pid),
        "pid": pid,
        "paused": bool(store.flag("paused")),
        "stop_requested": bool(store.flag("stop")),
        "kill_switch": str(settings.kill_switch),
        "killed": settings.kill_switch.exists(),
        "dry_run": bool(settings["dry_run"]),
        "repos": sorted(settings.repos),
        "tasks": counts,
        "today": {**budget.spent(), "caps": {k: settings.caps[k] for k in ("coding_runs_per_day", "tokens_per_day", "cost_usd_per_day")}},
        "coding": budget.may_code().reason,
        "waiting_on_approval": [
            {"task": s.task_id, "tool": s.args.get("tool", ""), "ticket": s.ticket_id} for s in store.waiting_steps()
        ],
        "needs_owner": [{"task": t.id, "title": t.title, "note": t.note} for t in store.tasks(["needs_owner"])],
        "cloud_blocked": store.flag("cloud_blocked_reason") if float(store.flag("cloud_blocked_until", "0") or 0) > time.time() else "",
        "local_today": store.usage_for(budget.today, "local")["runs"],
    }


def status_text(settings: Settings, store: Store) -> str:
    st = status(settings, store)
    state = "KILLED (kill switch present)" if st["killed"] else ("running" if st["running"] else "not running")
    if st["paused"]:
        state += ", paused"
    lines = [
        f"autopilot: {state}{' (dry run)' if st['dry_run'] else ''}",
        f"repos: {', '.join(st['repos']) or '(none configured)'}",
        "tasks: " + (", ".join(f"{k} {v}" for k, v in sorted(st["tasks"].items())) or "none"),
        f"today: {st['today']['runs']} coding runs, {st['today']['tokens']:,} tokens, ${st['today']['cost']:.2f} "
        f"(caps {st['today']['caps']['coding_runs_per_day']} / {st['today']['caps']['tokens_per_day']:,} / ${st['today']['caps']['cost_usd_per_day']}) - {st['coding']}",
    ]
    lines.append(f"local coding today: {st['local_today']} runs" + (f"; cloud blocked: {st['cloud_blocked']}" if st["cloud_blocked"] else ""))
    for item in st["waiting_on_approval"]:
        lines.append(f"  waiting: task #{item['task']} {item['tool']} (ticket {item['ticket']})")
    for item in st["needs_owner"]:
        lines.append(f"  needs you: #{item['task']} {item['title'][:70]} - {item['note'][:120]}")
    lines.append(f"kill switch: {st['kill_switch']}")
    return "\n".join(lines)


def list_text(store: Store, show_all: bool = False) -> str:
    tasks = store.tasks(None if show_all else OPEN_STATES)
    if not tasks:
        return "no tasks"
    out = []
    for t in sorted(tasks, key=lambda t: (t.state in ("done", "cancelled"), -t.value, t.id)):
        step = t.next_step()
        where = f"step {step.idx + 1}/{len(t.steps)} {step.kind}" if step else ""
        out.append(f"#{t.id:<4} {t.state:11} [{t.value:3.0f}] {t.repo or '-':16} {t.title[:60]:60} {where} {t.pr_url}")
    return "\n".join(out)


def log_text(store: Store, task_id: int) -> str:
    task = store.get(task_id)
    lines = [f"#{task.id} {task.title}", f"state {task.state} ({task.note}); repo {task.repo or '-'} branch {task.branch or '-'}",
             f"spent {task.coding_runs} runs, {task.tokens:,} tokens, ${task.cost:.2f}; attempts {task.attempts}; PR {task.pr_url or '-'}", "", "plan:"]
    for s in task.steps:
        lines.append(f"  {s.idx + 1:2}. [{s.state:8}] {s.kind:11} {s.title}" + (f" (ticket {s.ticket_id})" if s.ticket_id else ""))
    lines += ["", "log:"]
    for e in store.events(task_id):
        lines.append(f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(e['ts']))} {e['kind']:8} {e['message'][:300]}")
    return "\n".join(lines)


def retry(store: Store, task_id: int) -> None:
    state = "ready" if store.get(task_id).steps else "queued"
    store.set_state(task_id, state, "retry requested by the owner", attempts=0, next_at=0)


def cancel(store: Store, task_id: int) -> list[str]:
    """Cancel a task. Returns ticket ids still open in game (the player can dismiss them there)."""
    open_tickets = []
    for step in store.steps(task_id):
        if step.state == "waiting":
            open_tickets.append(step.ticket_id)
            store.update_step(step.id, state="skipped", result="task cancelled by the owner")
    store.set_state(task_id, "cancelled", "cancelled by the owner")
    return open_tickets


def pool_text(pool: Pool) -> str:
    rows = pool.status()
    if not rows:
        return "no backends"
    return "\n".join(
        f"{r['name']:18} {'up  ' if r['healthy'] else 'DOWN'} {r['kind']:8} {r['model']:18} {','.join(r['roles']):24} busy {r['busy']}/{r['slots']}  {r['reason']}"
        for r in rows
    )
