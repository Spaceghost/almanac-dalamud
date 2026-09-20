"""The loop end to end with fakes: plan -> code -> test -> draft PR -> review -> CI, tickets, fallbacks, controls."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from autopilot_fakes import CLAUDE_RATE_LIMITED, FakeGame, FakeModel, make_pilot, make_pool, run_until

from almanac.autopilot import app
from almanac.autopilot.game import Ticket
from almanac.autopilot.pool import Backend, Pool, PoolModel
from almanac.autopilot.sandbox import SubprocessRunner


def _done(pilot, task_id):
    return lambda: pilot.store.get(task_id).state in ("done", "needs_owner", "cancelled")


def test_live_flow_opens_a_draft_pr_and_waits_for_ci(tmp_path: Path) -> None:
    pilot, proc, game, clock, audits = make_pilot(tmp_path)
    proc.tests = [1, 0]  # first run fails -> a fix step is inserted
    proc.checks = ["pending", "pass"]
    tid, _ = app.add(pilot.store, "Fix the resize flicker", "demo")
    run_until(pilot, clock, _done(pilot, tid))
    task = pilot.store.get(tid)
    assert task.state == "done", task.note
    assert task.pr_url == "https://github.com/o/demo/pull/7"
    kinds = [(s.kind, s.state) for s in task.steps]
    assert kinds == [
        ("code", "done"), ("test", "skipped"), ("code", "done"), ("test", "done"),
        ("pr", "done"), ("review", "done"), ("ci", "done"),
    ]
    # cloud sessions ran in the task worktree, never in the main checkout
    claude_calls = [c for c in proc.calls if c["argv"][0] == "claude"]
    assert len(claude_calls) == 2
    assert all(str(c["cwd"]).startswith(str(tmp_path / "ap" / "worktrees")) for c in claude_calls)
    assert "--max-turns" in claude_calls[0]["argv"] and "Bash(git push:*)" in claude_calls[0]["argv"][claude_calls[0]["argv"].index("--disallowedTools") + 1]
    # the session got only allow-listed environment (no stray secrets)
    assert "SECRET_THING" not in claude_calls[0]["env"]
    # push went to the task branch only, without --force; PR is a draft with author labels
    (push,) = [a for a in proc.argvs("git") if "push" in a]
    assert push[-1] == f"HEAD:refs/heads/{task.branch}" and task.branch.startswith("autopilot/") and "--force" not in push
    (create,) = proc.argvs("gh pr create")
    assert "--draft" in create and create[create.index("--label", create.index("--label") + 1) + 1] == "by:claude"
    body = next(c["stdin"] for c in proc.calls if c["argv"][:3] == ["gh", "pr", "create"])
    assert "Test evidence" in body and "ok 12 tests" in body and "Written by: claude" in body
    # budget charged, review commented, audit trail present
    assert pilot.budget.spent()["runs"] == 2 and task.tokens == 2 * 1600
    assert proc.argvs("gh pr comment")
    events = {a[0][0] for a in audits}
    assert {"coding_session", "draft_pr", "step_failed"} <= events
    # worktree removed at the end
    assert proc.argvs("git -C " + str(tmp_path / "repo") + " worktree remove")


def test_dry_run_never_runs_coders_pushes_or_opens_prs(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = make_pilot(tmp_path, dry_run=True)
    tid, _ = app.add(pilot.store, "Try something", "demo")
    run_until(pilot, clock, _done(pilot, tid))
    assert pilot.store.get(tid).state == "done"
    ran = {c["argv"][0] for c in proc.calls}
    assert "claude" not in ran and "codex" not in ran and "aider" not in ran
    assert not [a for a in proc.argvs("git") if "push" in a]
    assert not proc.argvs("gh pr create") and not proc.argvs("gh pr comment")
    assert any("dry-run: would run gh pr create" in e["message"] for e in pilot.store.events())


def test_game_action_parks_other_work_continues_then_resumes(tmp_path: Path) -> None:
    plan = json.dumps({"steps": [
        {"kind": "game_action", "title": "Reload the plugin", "args": {"tool": "reload_plugin", "args": {"name": "ghostty"}, "reason": "load the new build"}},
        {"kind": "local", "title": "Summarize", "args": {"prompt": "summarize"}},
    ]})
    game = FakeGame(action_tools={"reload_plugin"})
    pilot, proc, game, clock, audits = make_pilot(tmp_path, planner_model=FakeModel([plan]), game=game)
    first, _ = app.add(pilot.store, "Reload after build", "", priority=90)
    pilot.tick()  # plan
    pilot.tick()  # request_action -> ticket, parked
    task = pilot.store.get(first)
    assert task.state == "waiting" and task.blockers == ["t1"]
    assert game.requests[0]["resume_token"] == f"autopilot:{first}:{task.steps[0].id}"
    assert any("Approve reload_plugin" in item for item in game.objectives[-1])
    # other work continues while parked
    second, _ = app.add(pilot.store, "Unrelated triage", "", priority=10)
    run_until(pilot, clock, _done(pilot, second))
    assert pilot.store.get(first).state == "waiting"
    # the owner approves later; the plan resumes exactly after the parked step
    game.tickets["t1"] = Ticket("t1", "approved", '{"reloaded": true}')
    run_until(pilot, clock, _done(pilot, first))
    steps = pilot.store.get(first).steps
    assert [(s.kind, s.state) for s in steps] == [("game_action", "done"), ("local", "done")]
    assert "reloaded" in steps[0].result
    assert len(game.requests) == 1  # never re-requested
    assert any(a[0][0] == "ticket_resolved" for a in audits)


def test_denied_ticket_replans_without_that_action(tmp_path: Path) -> None:
    plan = json.dumps({"steps": [{"kind": "game_action", "title": "Reload", "args": {"tool": "reload_plugin", "args": {}}}]})
    replan = json.dumps({"steps": [{"kind": "game_action", "args": {"tool": "reload_plugin", "args": {}}}, {"kind": "local", "title": "Tell the owner"}]})
    game = FakeGame(action_tools={"reload_plugin"})
    pilot, proc, game, clock, _ = make_pilot(tmp_path, planner_model=FakeModel([plan, replan]), game=game)
    tid, _ = app.add(pilot.store, "Reload", "")
    pilot.tick()
    pilot.tick()
    game.tickets["t1"] = Ticket("t1", "denied")
    run_until(pilot, clock, _done(pilot, tid))
    steps = pilot.store.get(tid).steps
    assert [(s.kind, s.state) for s in steps] == [("game_action", "skipped"), ("local", "done")]
    assert len(game.requests) == 1


def test_game_actions_never_requested_in_dry_run(tmp_path: Path) -> None:
    plan = json.dumps({"steps": [{"kind": "game_action", "args": {"tool": "reload_plugin", "args": {}}}]})
    pilot, proc, game, clock, _ = make_pilot(tmp_path, planner_model=FakeModel([plan]), game=FakeGame(action_tools={"reload_plugin"}), dry_run=True)
    tid, _ = app.add(pilot.store, "Reload", "")
    run_until(pilot, clock, _done(pilot, tid))
    assert game.requests == []


def test_rate_limit_switches_to_a_local_coder_and_labels_the_pr(tmp_path: Path) -> None:
    pilot, proc, game, clock, audits = make_pilot(tmp_path)
    proc.claude = [(1, CLAUDE_RATE_LIMITED)]
    tid, _ = app.add(pilot.store, "Fix it", "demo")
    run_until(pilot, clock, _done(pilot, tid), step_seconds=60)
    task = pilot.store.get(tid)
    assert task.state == "done"
    (codex,) = proc.argvs("codex")
    assert codex[:2] == ["codex", "exec"] and "-m" in codex and codex[codex.index("-m") + 1] == "qwen-coder"
    env = next(c["env"] for c in proc.calls if c["argv"][0] == "codex")
    assert env["OPENAI_API_BASE"] == "http://gpu-a/v1"
    # codex runs with autopilot's own CODEX_HOME, not the owner's ~/.codex
    assert env["CODEX_HOME"] == str(pilot.s.codex_home) and Path(env["CODEX_HOME"]).is_dir()
    assert task.authors == "local:qwen-coder@gpu-a"
    (create,) = proc.argvs("gh pr create")
    assert "by:local-qwen-coder-gpu-a" in create
    assert pilot.store.flag("cloud_blocked_reason") == "claude rate_limit"
    assert any(a[0][0] == "cloud_limited" for a in audits)
    # while blocked, the next coding task goes straight to the local coder
    t2, _ = app.add(pilot.store, "Second fix", "demo")
    run_until(pilot, clock, _done(pilot, t2), step_seconds=60)
    assert len([c for c in proc.calls if c["argv"][0] == "claude"]) == 1
    assert len(proc.argvs("codex")) == 2
    # after the block expires, cloud coding is used again
    clock.advance(3600)
    t3, _ = app.add(pilot.store, "Third fix", "demo")
    run_until(pilot, clock, _done(pilot, t3), step_seconds=60)
    assert len([c for c in proc.calls if c["argv"][0] == "claude"]) == 2


def test_sandboxed_local_session_keeps_the_network_and_its_own_codex_home(tmp_path: Path) -> None:
    # A repo may forbid the network for cloud sessions and tests; a local coding
    # session still has to reach the model backend, and codex has to write its
    # own CODEX_HOME.
    pilot, proc, game, clock, _ = make_pilot(
        tmp_path, caps={"coding_runs_per_day": 0}, sandbox={"mode": "bwrap"},
        repos={"demo": {"path": str(tmp_path / "repo"), "github": "o/demo", "test": ["tests/run.sh"], "allow_network": False}},
    )
    tid, _ = app.add(pilot.store, "Fix it", "demo")
    run_until(pilot, clock, _done(pilot, tid), step_seconds=60)
    session = next(c["argv"] for c in proc.calls if "codex" in c["argv"])
    assert session[0] == "bwrap" and "--unshare-net" not in session
    binds = [session[i + 1] for i, a in enumerate(session) if a == "--bind"]
    assert str(pilot.s.codex_home) in binds
    # the repo's own test run still obeys the repo: no network
    tests = next(c["argv"] for c in proc.calls if "tests/run.sh" in c["argv"])
    assert tests[0] == "bwrap" and "--unshare-net" in tests


def test_daily_cap_uses_local_coder_with_smaller_split_steps(tmp_path: Path) -> None:
    split = FakeModel([json.dumps({"subtasks": ["add the parser", "wire it in"]})],
                      default=json.dumps({"verdict": "approve", "summary": "ok", "comments": [], "tests_to_add": []}))
    pilot, proc, game, clock, _ = make_pilot(tmp_path, review=split, caps={"coding_runs_per_day": 0, "local_split_parts": 3})
    tid, _ = app.add(pilot.store, "Add a config parser", "demo")
    run_until(pilot, clock, _done(pilot, tid))
    task = pilot.store.get(tid)
    assert task.state == "done"
    assert [c for c in proc.calls if c["argv"][0] == "claude"] == []
    prompts = [a[-1] for a in proc.argvs("codex")]
    assert len(prompts) == 2 and "add the parser" in prompts[0] and "wire it in" in prompts[1]
    assert [s.kind for s in task.steps][:6] == ["code", "code", "test", "code", "test", "test"]


def test_no_backend_and_no_cloud_defers_without_counting_a_failure(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = make_pilot(tmp_path, caps={"coding_runs_per_day": 0})
    pilot.pool = make_pool(clock, down={"gpu-a"})
    tid, _ = app.add(pilot.store, "Fix it", "demo")
    pilot.tick()
    pilot.tick()
    task = pilot.store.get(tid)
    assert task.attempts == 0 and task.next_at > clock() and "no local coder backend" in task.note


def test_review_requests_changes_and_a_fix_round_follows(tmp_path: Path) -> None:
    changes = json.dumps({"verdict": "changes", "summary": "missing edge case", "comments": ["foo.py:3 - handle 0"], "tests_to_add": ["zero width"]})
    approve = json.dumps({"verdict": "approve", "summary": "good", "comments": [], "tests_to_add": []})
    pilot, proc, game, clock, _ = make_pilot(tmp_path, review=FakeModel([changes], default=approve))
    tid, _ = app.add(pilot.store, "Fix it", "demo")
    run_until(pilot, clock, _done(pilot, tid))
    task = pilot.store.get(tid)
    assert [s.kind for s in task.steps] == ["code", "test", "pr", "review", "code", "test", "pr", "review", "ci"]
    fix_prompt = [c["argv"] for c in proc.calls if c["argv"][0] == "claude"][1][2]
    assert "handle 0" in fix_prompt and "zero width" in fix_prompt
    assert len(proc.argvs("gh pr create")) == 1  # the second pr step pushes to the same PR
    assert len(proc.argvs("gh pr comment")) == 2


def test_parallel_cloud_and_local_workers(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = make_pilot(tmp_path, caps={"max_parallel": 3})
    a, _ = app.add(pilot.store, "A", "demo", priority=90)
    b, _ = app.add(pilot.store, "B", "demo", priority=80)
    pilot.tick()  # plans both
    pilot.tick()  # both code steps start in the same tick: A on the cloud, B on the local backend
    assert pilot.store.get(a).authors == "claude"
    assert pilot.store.get(b).authors == "local:qwen-coder@gpu-a"


def test_failures_back_off_then_go_to_the_owner(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = make_pilot(tmp_path, max_attempts=2)
    proc.claude = [(1, "Error: something broke"), (1, "Error: again")]
    tid, _ = app.add(pilot.store, "Fix it", "demo")
    pilot.tick()
    pilot.tick()
    task = pilot.store.get(tid)
    assert task.attempts == 1 and task.next_at == clock() + 60
    pilot.tick()  # still backing off: nothing happens
    assert len([c for c in proc.calls if c["argv"][0] == "claude"]) == 1
    clock.advance(61)
    pilot.tick()
    task = pilot.store.get(tid)
    assert task.state == "needs_owner" and "failed 2 times" in task.note
    assert any("Needs you: #" in i for i in game.objectives[-1])
    app.retry(pilot.store, tid)
    assert pilot.store.get(tid).state == "ready" and pilot.store.get(tid).attempts == 0


def test_repo_without_remote_keeps_the_branch_local(tmp_path: Path) -> None:
    repos = {"local-only": {"path": str(tmp_path / "repo"), "remote": "", "test": ["tests/run.sh"]}}
    pilot, proc, game, clock, _ = make_pilot(tmp_path, repos=repos)
    tid, _ = app.add(pilot.store, "Tidy", "local-only")
    run_until(pilot, clock, _done(pilot, tid))
    task = pilot.store.get(tid)
    assert task.state == "needs_owner" and "ready locally" in task.note
    assert not [a for a in proc.argvs("git") if "fetch" in a or "push" in a] and not proc.argvs("gh")
    (add,) = [a for a in proc.argvs("git") if "worktree" in a and "add" in a]
    assert add[-1] == "master"


def test_unlisted_repo_goes_to_owner(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = make_pilot(tmp_path)
    tid, _ = pilot.store.add_task("github", "x", repo="not-allowed")
    pilot.tick()
    assert pilot.store.get(tid).state == "needs_owner"


def test_controls_pause_stop_kill(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = make_pilot(tmp_path)
    app.add(pilot.store, "x", "demo")
    app.pause(pilot.store, True)
    assert pilot.tick() == "paused" and proc.calls == []
    app.pause(pilot.store, False)
    assert pilot.tick() == "working"
    app.stop(pilot.store)
    assert pilot.tick() == "stopped"
    pilot.store.set_flag("stop", "")
    pilot.s.kill_switch.write_text("stop")
    assert pilot.tick() == "killed"
    assert pilot.run_forever() == 3


def test_secrets_are_redacted_from_logs(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = make_pilot(tmp_path, secrets={"ANTHROPIC_API_KEY": "env:ANTHROPIC_API_KEY"})
    proc.claude = [(1, "Error: auth header was sk-test-value-1234 oops")]
    tid, _ = app.add(pilot.store, "x", "demo")
    pilot.tick()
    pilot.tick()
    env = next(c["env"] for c in proc.calls if c["argv"][0] == "claude")
    assert env["ANTHROPIC_API_KEY"] == "sk-test-value-1234"
    text = json.dumps(pilot.store.events()) + json.dumps([s.result for s in pilot.store.get(tid).steps])
    assert "sk-test-value-1234" not in text and "***" in text


def test_digest_lists_prs_spend_and_waiting(tmp_path: Path) -> None:
    pilot, proc, game, clock, _ = make_pilot(tmp_path)
    tid, _ = app.add(pilot.store, "Fix the thing", "demo")
    run_until(pilot, clock, _done(pilot, tid))
    path = pilot.maybe_digest(force=True)
    text = path.read_text()
    assert "https://github.com/o/demo/pull/7" in text and "cloud coding: 1 run(s)" in text and "gpu-a" in text
    assert game.toasts and game.toasts[-1].startswith("autopilot: 1 done")


def test_status_and_mcp_tools(tmp_path: Path, config) -> None:
    from almanac.service import Almanac

    alm = Almanac(config)
    names = {t["name"]: t["safety"] for t in alm.catalogue()}
    assert names["autopilot_status"] == "read" and names["autopilot_add"] == "change" and names["autopilot_pause"] == "change"
    first = alm.call("autopilot_add", {"text": "write docs"}, caller="test")
    assert first.needs_confirmation and "Queue an autopilot task" in first.plan
    settings, store = app.open_store(config)
    assert store.tasks() == []
    done = alm.call("autopilot_add", {"text": "write docs", "confirm": first.token}, caller="test")
    assert "queued autopilot task #1" in done.text
    status = json.loads(alm.call("autopilot_status", {}, caller="test").text)
    assert status["tasks"] == {"queued": 1} and status["dry_run"] is True
    bad = alm.call("autopilot_add", {"text": "x", "repo": "nope"}, caller="test")
    assert bad.is_error


def test_pool_roles_process_rule_failover_and_leases(tmp_path: Path) -> None:
    from autopilot_fakes import Clock, Resp

    clock = Clock()
    running: set[str] = set()
    events: list[str] = []
    backends = [
        Backend("box-a", "http://box-a", "almanac", "qwen", roles=["planner", "coder", "reviewer"], priority=10),
        Backend("box-b", "http://box-b", "almanac", "qwen", roles=["planner", "reviewer"], priority=5),
        Backend("box-c", "http://box-c", "ollama", "qwen", roles=["coder", "reviewer"], unavailable_while_process=["ffxiv_dx11.exe"]),
    ]
    unloads: list[str] = []
    pool = Pool(backends, get=lambda url, **k: Resp(200), post=lambda url, **k: unloads.append(url), match=lambda wanted: [w for w in wanted if w in running],
                clock=clock, on_event=events.append)
    lease = pool.acquire("coder")
    assert lease.backend.name == "box-a"
    assert pool.acquire("coder").backend.name == "box-c"
    assert pool.acquire("coder") is None  # both coder slots busy
    pool.release(lease)
    reviewer = pool.acquire("reviewer", avoid={"box-a"})
    assert reviewer.backend.name == "box-b"
    running.add("ffxiv_dx11.exe")
    clock.advance(60)
    assert "box-c" not in [b.name for b in pool.healthy("coder")]
    assert pool.abort_reason(type(lease)(backends[2], "coder"))().startswith("off-limits while ffxiv_dx11.exe")
    assert any("box-c: down" in e for e in events)
    assert unloads and all(u == "http://box-c/api/generate" for u in unloads)  # the game gets its GPU back

    class Flaky:
        def __init__(self, b):
            self.b = b

        def complete(self, system, user, json_mode=False):
            from almanac.autopilot.planner import ModelUnavailable

            if self.b.name == "box-a":
                raise ModelUnavailable("box-a timed out")
            return f"answer from {self.b.name}"

    model = PoolModel(pool, "planner", factory=Flaky)
    assert model.complete("s", "u") == "answer from box-b"


def test_subprocess_runner_kill_switch_and_timeout(tmp_path: Path) -> None:
    kill = tmp_path / "STOP"
    runner = SubprocessRunner(kill)
    sleeper = [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"]
    start = time.monotonic()
    result = runner(sleeper, tmp_path, {"PATH": "/usr/bin"}, timeout=1.5)
    assert result.stopped == "timeout" and time.monotonic() - start < 15 and "started" in result.output
    threading.Timer(0.5, kill.write_text, args=("x",)).start()
    result = runner(sleeper, tmp_path, {"PATH": "/usr/bin"}, timeout=60)
    assert result.stopped == "kill switch"
    kill.unlink()
    result = runner(sleeper, tmp_path, {"PATH": "/usr/bin"}, timeout=60, abort=lambda: "game started")
    assert result.stopped == "aborted: game started"
    ok = runner([sys.executable, "-c", "print('hi')"], tmp_path, {"PATH": "/usr/bin"}, timeout=10)
    assert ok.ok and ok.output.strip() == "hi"
    assert subprocess.run(["true"]).returncode == 0
