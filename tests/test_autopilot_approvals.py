"""Owner approval: code and push gates, allow sessions, persistence across a restart, hard caps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from almanac.autopilot import app
from almanac.autopilot.approvals import Approvals
from almanac.autopilot.git import files_changed
from almanac.autopilot.runner import Autopilot, InlineExecutor
from almanac.autopilot.settings import DEFAULTS, Repo, Settings, _merge
from almanac.autopilot.store import Store
from autopilot_fakes import CLAUDE_OK, Clock, FakeGame, FakeModel, FakeProc, make_pilot, make_pool, run_until

PLAN = json.dumps({"steps": [
    {"kind": "code", "title": "Make the change", "args": {"prompt": "add a flag"}},
    {"kind": "test", "title": "Run the tests", "args": {}},
    {"kind": "pr", "title": "Open a draft PR", "args": {}},
]})


GATES = {"require": ["code", "push", "game_action"]}  # what the shipped defaults ask for


def pilot_with_plan(tmp_path: Path, **over):
    """A pilot with the real approval policy on (the loop fakes switch it off)."""
    planner = FakeModel([PLAN], default=PLAN)
    over["approval"] = {**GATES, **over.get("approval", {})}
    return make_pilot(tmp_path, planner_model=planner, **over)


def test_shipped_defaults_gate_code_pushes_and_game_actions() -> None:
    assert DEFAULTS["approval"]["require"] == ["code", "push", "game_action"]
    assert DEFAULTS["approval"]["allow_session_minutes"] == 5
    assert DEFAULTS["approval"]["code_scope"] == "task"
    assert DEFAULTS["dry_run"] is True


def test_code_step_waits_for_approval_and_nothing_runs_meanwhile(tmp_path: Path) -> None:
    pilot, proc, game, clock, audits = pilot_with_plan(tmp_path)
    task_id, _ = app.add(pilot.store, "Add a flag", "demo")
    pilot.tick()  # plans
    pilot.tick()  # code step asks for approval and parks
    task = pilot.store.get(task_id)
    assert task.state == "waiting"
    pending = pilot.approvals.pending()
    assert [(a.kind, a.task_id, a.state) for a in pending] == [("code", task_id, "pending")]
    assert "add a flag" in pending[0].detail  # the owner sees what the session is asked to do
    assert not proc.argvs("claude")  # no session started
    # more ticks change nothing while it waits
    pilot.tick()
    pilot.tick()
    assert not proc.argvs("claude")
    assert pilot.store.get(task_id).state == "waiting"
    assert any(a[0][0] == "approval_requested" for a in audits)
    assert any("waiting for your approval" in s for s in game.statuses)


def test_approval_resumes_the_same_step_after_a_restart(tmp_path: Path) -> None:
    """The owner answers hours later, after a reboot: the parked step runs, the plan continues."""
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path)
    task_id, _ = app.add(pilot.store, "Add a flag", "demo")
    pilot.tick()
    pilot.tick()
    ticket = pilot.approvals.pending()[0].ticket
    db_path = pilot.store.path
    pilot.store.close()  # the service stops (or the machine reboots)

    # a fresh process answers the approval: only the queue file connects the two
    settings2 = pilot.s
    store2 = Store(db_path, clock=clock)
    approved = app.answer(settings2, store2, ticket, "approved")
    assert [(a.ticket, a.state) for a in approved] == [(ticket, "approved")]
    store2.close()

    # the loop comes back up
    store3 = Store(db_path, clock=clock)
    assert store3.recover() == 0
    pilot2 = Autopilot(
        settings2, store3, pilot.planner, game, proc, lambda *a, **k: None, pool=make_pool(clock),
        environ={"PATH": "/usr/bin", "HOME": str(tmp_path)}, clock=clock, executor=InlineExecutor(),
        backend_model=lambda b: FakeModel(default=json.dumps({"verdict": "approve", "summary": "ok", "comments": [], "tests_to_add": []})),
        which=lambda _n: None,
    )
    pilot2.poll_tickets()
    task = store3.get(task_id)
    assert task.state == "ready"
    step = task.next_step()
    assert step is not None and step.kind == "code" and step.state == "pending" and step.ticket_id == ""
    pilot2.tick()
    assert proc.argvs("claude"), "the approved coding session ran after the restart"
    assert store3.get(task_id).steps[0].state == "done"


def test_push_needs_its_own_approval_and_a_denial_stops_the_task(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path)
    task_id, _ = app.add(pilot.store, "Add a flag", "demo")
    run_until(pilot, clock, lambda: bool(pilot.approvals.pending()))
    pilot.approvals.approve(pilot.approvals.pending()[0].ticket)  # the code gate
    run_until(pilot, clock, lambda: any(a.kind == "push" for a in pilot.approvals.pending()))
    push = [a for a in pilot.approvals.pending() if a.kind == "push"][0]
    assert "foo.py" in push.detail and "draft PR" in push.detail
    assert not proc.ran("push") and not proc.argvs("gh pr create")
    pilot.approvals.deny(push.ticket, reason="not tonight")
    pilot.poll_tickets()
    task = pilot.store.get(task_id)
    assert task.state == "needs_owner" and "you denied push" in task.note
    assert not proc.ran("push")


def test_allow_session_approves_as_requests_arrive_then_expires(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path)
    task_id, _ = app.add(pilot.store, "Add a flag", "demo")
    until, settled = pilot.approvals.open_allow("all", 5)
    assert until == clock() + 300 and settled == []
    run_until(pilot, clock, lambda: pilot.store.get(task_id).pr_url != "", step_seconds=30)
    task = pilot.store.get(task_id)
    assert task.pr_url and proc.ran("push")
    assert [a.state for a in pilot.approvals.recent()] == ["approved", "approved"]
    assert all(a.actor == "allow-session" for a in pilot.approvals.recent())

    # the window closes: the next task parks again
    clock.advance(600)
    assert pilot.approvals.allow_until("code") == 0
    app.add(pilot.store, "Another change", "demo")
    run_until(pilot, clock, lambda: bool(pilot.approvals.pending()), step_seconds=30)
    assert pilot.approvals.pending()[0].kind == "code"


def test_one_approval_covers_a_tasks_later_coding_steps(tmp_path: Path) -> None:
    """code_scope = task: the owner says yes once and that task may keep coding (all night)."""
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path)
    proc.tests = [1, 0]  # the first test run fails, so a second code step is inserted
    task_id, _ = app.add(pilot.store, "Add a flag", "demo")
    run_until(pilot, clock, lambda: bool(pilot.approvals.pending()))
    pilot.approvals.approve(pilot.approvals.pending()[0].ticket)
    run_until(pilot, clock, lambda: any(a.kind == "push" for a in pilot.approvals.pending()), step_seconds=30)
    assert len(proc.argvs("claude")) == 2, "both coding sessions ran on one approval"
    assert [a.kind for a in pilot.approvals.recent()] == ["push", "code"]


def test_code_scope_step_asks_every_time(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path, approval={"code_scope": "step"})
    proc.tests = [1, 0]
    task_id, _ = app.add(pilot.store, "Add a flag", "demo")
    run_until(pilot, clock, lambda: bool(pilot.approvals.pending()))
    pilot.approvals.approve(pilot.approvals.pending()[0].ticket)
    run_until(pilot, clock, lambda: len([a for a in pilot.approvals.recent() if a.kind == "code"]) == 2, step_seconds=30)
    assert len(proc.argvs("claude")) == 1, "the second session waits for its own approval"


def test_approval_can_be_switched_off_per_kind(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path, approval={"require": ["push"]})
    app.add(pilot.store, "Add a flag", "demo")
    run_until(pilot, clock, lambda: any(a.kind == "push" for a in pilot.approvals.pending()), step_seconds=30)
    assert proc.argvs("claude"), "coding ran without asking; only the push is gated"


def test_pending_and_status_text_tell_the_owner_what_to_do(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path)
    app.add(pilot.store, "Add a flag", "demo")
    pilot.tick()
    pilot.tick()
    text = app.pending_text(pilot.s, pilot.store)
    assert "ap-1" in text and "code" in text and "almanac autopilot approve" in text
    status = app.status_text(pilot.s, pilot.store)
    assert "needs approval: ap-1" in status and "approval required for: code, game_action, push" in status
    detail = app.show_text(pilot.s, pilot.store, "ap-1")
    assert "add a flag" in detail and "throwaway worktree" in detail
    # objectives shown in game include the approval
    pilot.sync_objectives()
    assert any("Approve code" in item for group in game.objectives for item in group)


def test_answer_all_and_allow_settle_everything_pending(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path, caps={"max_parallel": 2})
    app.add(pilot.store, "One", "demo")
    app.add(pilot.store, "Two", "demo")
    run_until(pilot, clock, lambda: len(pilot.approvals.pending()) == 2)
    until, settled = app.allow(pilot.s, pilot.store, "code", 5)
    assert sorted(a.ticket for a in settled) == ["ap-1", "ap-2"]
    assert all(a.approved for a in pilot.approvals.recent())


def test_denied_code_gate_never_runs_the_session(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path)
    task_id, _ = app.add(pilot.store, "Add a flag", "demo")
    run_until(pilot, clock, lambda: bool(pilot.approvals.pending()))
    pilot.approvals.deny(pilot.approvals.pending()[0].ticket, reason="wrong repo")
    pilot.poll_tickets()
    assert pilot.store.get(task_id).state == "needs_owner"
    pilot.tick()
    assert not proc.argvs("claude")
    # and a re-ask is not auto-approved by an old denial: the owner has to retry the task
    app.retry(pilot.store, task_id)
    pilot.tick()
    assert not proc.argvs("claude")


def test_max_files_per_task_sends_a_big_branch_to_the_owner(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path, caps={"max_files_per_task": 2})
    proc.stat = " a.py | 2 +-\n b.py | 3 +--\n c.py | 1 +\n 3 files changed, 6 insertions(+)\n"
    task_id, _ = app.add(pilot.store, "Add a flag", "demo")
    run_until(pilot, clock, lambda: bool(pilot.approvals.pending()))
    pilot.approvals.approve(pilot.approvals.pending()[0].ticket)
    run_until(pilot, clock, lambda: pilot.store.get(task_id).state == "needs_owner", step_seconds=30)
    task = pilot.store.get(task_id)
    assert "touches 3 files (cap 2)" in task.note
    assert not proc.ran("push")
    assert not any(a.kind == "push" for a in pilot.approvals.recent()), "it never even asks: the owner reads it by hand"


def test_low_memory_defers_coding_and_tests_without_failing(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path, caps={"min_free_memory_mb": 2000})
    pilot.free_memory_mb = lambda: 300
    task_id, _ = app.add(pilot.store, "Add a flag", "demo")
    pilot.tick()
    pilot.tick()
    task = pilot.store.get(task_id)
    assert task.attempts == 0 and "300 MB free" in task.note and task.next_at > clock()
    assert not pilot.approvals.pending() and not proc.argvs("claude")
    pilot.free_memory_mb = lambda: 8000
    clock.advance(700)
    pilot.tick()
    assert pilot.approvals.pending(), "with memory free again it asks for approval and goes on"


def test_repos_inside_a_denied_path_are_dropped(tmp_path: Path) -> None:
    raw = _merge(DEFAULTS, {
        "deny_paths": [str(tmp_path / "secret"), "~/.ssh"],
        "repos": {
            "ok": {"path": str(tmp_path / "work" / "repo")},
            "secrets": {"path": str(tmp_path / "secret" / "vault")},
            "self": {"path": str(tmp_path / "secret")},
        },
    })
    for name in ("work/repo", "secret/vault"):
        (tmp_path / name).mkdir(parents=True)
    settings = Settings(raw=raw, state_dir=tmp_path / "state")
    for name, spec in raw["repos"].items():
        repo = Repo.from_config(name, spec, "claude")
        if settings.forbids(repo.path):
            settings.denied[name] = settings.forbids(repo.path)
        else:
            settings.repos[name] = repo
    assert sorted(settings.repos) == ["ok"]
    assert sorted(settings.denied) == ["secrets", "self"]


def test_unknown_or_denied_repo_task_goes_to_the_owner(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path)
    pilot.s.denied["secret-repo"] = "~/.config"
    task_id, _ = pilot.store.add_task("manual", "Touch the secrets", repo="secret-repo")
    pilot.tick()
    assert pilot.store.get(task_id).state == "needs_owner"
    assert not proc.argvs("claude")


def test_durable_action_log_records_every_step(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path)
    task_id, _ = app.add(pilot.store, "Add a flag", "demo", settings=pilot.s)
    pilot.tick()
    pilot.tick()
    text = pilot.s.log_file.read_text()
    assert "queued by the owner: Add a flag" in text
    assert "approval" in text and "ap-1 code needs you" in text
    assert f"#{task_id}" in text
    # the file is append-only across restarts
    app.answer(pilot.s, pilot.store, "ap-1", "approved")
    assert "ap-1 code approved by owner" in pilot.s.log_file.read_text()


def test_secrets_never_reach_the_approval_detail_or_log(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = make_pilot(
        tmp_path, approval=GATES,
        planner_model=FakeModel([json.dumps({"steps": [{"kind": "code", "title": "x", "args": {"prompt": "use sk-test-value-1234 please"}}]})]),
    )
    app.add(pilot.store, "Add a flag", "demo", settings=pilot.s)
    pilot.tick()
    pilot.tick()
    assert "sk-test-value-1234" not in pilot.s.log_file.read_text()


# -- cloud limits: the fallback the owner asked to see proven ------------------

@pytest.mark.parametrize("output", [
    'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"rate limit"}}',
    "Claude usage limit reached. Your limit will reset at 3pm.",
    '{"type":"error","error":{"type":"insufficient_quota","message":"You exceeded your current quota"}}',
    "Invalid API key. Please run /login",
])
def test_every_cloud_limit_shape_falls_back_to_a_local_coder(tmp_path: Path, output: str) -> None:
    plan = json.dumps({"steps": [{"kind": "code", "title": "Change it", "args": {"prompt": "do it"}}]})
    pilot, proc, game, clock, audits = make_pilot(tmp_path, planner_model=FakeModel([plan], default=plan))
    proc.claude = [(1, json.dumps({"type": "result", "subtype": "error", "is_error": True, "num_turns": 1, "result": output, "usage": {}}))]
    task_id, _ = app.add(pilot.store, "Change it", "demo")
    run_until(pilot, clock, lambda: pilot.store.get(task_id).state == "done", step_seconds=30)
    task = pilot.store.get(task_id)
    assert task.authors == "local:qwen-coder@gpu-a", "the local coder finished the work"
    assert pilot.store.flag("cloud_blocked_reason").startswith("claude ")
    assert float(pilot.store.flag("cloud_blocked_until")) > clock()
    assert any(a[0][0] == "cloud_limited" for a in audits)
    assert proc.argvs("aider") or proc.argvs("codex"), "a local session ran"


def test_cloud_limit_keeps_the_gate_in_place(tmp_path: Path) -> None:
    """Falling back to a local coder is not a way around the approval gate."""
    plan = json.dumps({"steps": [{"kind": "code", "title": "Change it", "args": {"prompt": "do it"}}]})
    pilot, proc, game, clock, _ = make_pilot(tmp_path, planner_model=FakeModel([plan], default=plan), approval=GATES)
    pilot.store.set_flag("cloud_blocked_until", str(clock() + 3600))
    pilot.store.set_flag("cloud_blocked_reason", "claude usage_limit")
    app.add(pilot.store, "Change it", "demo")
    run_until(pilot, clock, lambda: bool(pilot.approvals.pending()))
    assert not proc.argvs("aider") and not proc.argvs("claude")
    assert pilot.approvals.pending()[0].kind == "code"
    assert "local" in pilot.approvals.pending()[0].detail  # the owner is told who would write it


# -- small units ---------------------------------------------------------------

def test_files_changed_counts_stat_lines() -> None:
    assert files_changed(" a.py | 2 +-\n b.py | 3 +--\n 2 files changed, 5 insertions(+)\n") == 2
    assert files_changed("") == 0


def test_allow_session_is_capped_and_scoped(tmp_path: Path) -> None:
    store = Store(":memory:")
    approvals = Approvals(store, {"require": ["code", "push"], "max_allow_session_minutes": 10}, clock=lambda: 1000.0)
    until, _ = approvals.open_allow("code", 600)
    assert until == 1000.0 + 600  # 10 minutes, not 600
    assert approvals.allow_until("code") == until
    assert approvals.allow_until("push") == 0  # a code session does not cover pushes
    approvals.close_allow("code")
    assert approvals.allow_until("code") == 0


def test_approval_rows_survive_and_are_queryable(tmp_path: Path) -> None:
    path = tmp_path / "q.sqlite"
    store = Store(path)
    approvals = Approvals(store, {"require": ["code"]}, clock=lambda: 10.0)
    first = approvals.request("code", 1, 1, "Edit demo", "detail", "code:1")
    again = approvals.request("code", 1, 2, "Edit demo again", "detail", "code:1")
    assert again.ticket == first.ticket, "the same scope parks on the same ticket"
    store.close()
    reopened = Store(path)
    back = Approvals(reopened, {"require": ["code"]})
    assert [a.ticket for a in back.pending()] == [first.ticket]
    back.approve(first.ticket, "owner")
    assert back.get(first.ticket).approved and back.pending() == []
    assert back.request("code", 1, 3, "Edit demo a third time", "", "code:1").approved


def test_stop_is_noticed_between_ticks_not_after_the_whole_idle_period(tmp_path: Path) -> None:
    """`stop --now` should take seconds: the wait between ticks is sliced."""
    pilot, proc, game, clock, _ = pilot_with_plan(tmp_path, idle_seconds=120, tick_seconds=120)
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 1:  # the owner hits the kill switch while the loop idles
            pilot.s.kill_switch.write_text("stopped\n")

    assert pilot.run_forever(sleep=sleep) == 0
    assert slept == [5.0], "it stopped after one 5-second slice, not after 120 seconds"
