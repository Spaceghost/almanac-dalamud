"""Queue, budget, planning, sources and limit detection (pure logic, no processes)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from autopilot_fakes import Clock, FakeModel, make_settings

from almanac.autopilot.budget import Budget, backoff_seconds
from almanac.autopilot.coder import CodeResult, classify_limit, local_coder_argv, parse_claude, parse_codex
from almanac.autopilot.game import is_forbidden, ticket_from
from almanac.autopilot.git import GitError, GitOps, branch_for
from almanac.autopilot.planner import Planner, normalize_plan, review_diff, split_for_local
from almanac.autopilot.sandbox import ProcResult, Redactor, SecretError, bwrap_argv, child_env, resolve_secrets
from almanac.autopilot.sources import GhError, ReadOnlyGh, collect, failing_ci, github_issues, inbox, vote_ideas
from almanac.autopilot.store import Store


@pytest.fixture
def store() -> Store:
    return Store(":memory:", clock=Clock())


# -- store -----------------------------------------------------------------------

def test_add_dedupes_by_source_ref_and_picks_highest_value(store: Store) -> None:
    a, created = store.add_task("github", "low", value=10, source_ref="github:o/r#1")
    assert created
    assert store.add_task("github", "low again", value=99, source_ref="github:o/r#1") == (a, False)
    b, _ = store.add_task("manual", "high", value=80)
    assert store.pick().id == b
    store.update(b, next_at=store.clock() + 60)  # backing off
    assert store.pick().id == a
    store.set_state(a, "waiting")  # parked on a ticket
    assert store.pick() is None
    assert store.pick(exclude={a}) is None


def test_plan_replace_keeps_history_and_insert_shifts(store: Store) -> None:
    tid, _ = store.add_task("manual", "t")
    store.set_plan(tid, [{"kind": "code", "title": "c"}, {"kind": "test", "title": "t"}, {"kind": "pr", "title": "p"}])
    first = store.steps(tid)[0]
    store.update_step(first.id, state="done")
    store.append_steps(tid, [{"kind": "local", "title": "inserted"}], after_idx=0)
    assert [s.title for s in store.steps(tid)] == ["c", "inserted", "t", "p"]
    store.set_plan(tid, [{"kind": "local", "title": "new"}])
    assert [(s.title, s.state) for s in store.steps(tid)] == [("c", "done"), ("new", "pending")]


def test_recover_resets_running_steps(store: Store) -> None:
    tid, _ = store.add_task("manual", "t")
    store.set_plan(tid, [{"kind": "local", "title": "x"}])
    store.update_step(store.steps(tid)[0].id, state="running")
    assert store.recover() == 1
    assert store.steps(tid)[0].state == "pending"


# -- budget ------------------------------------------------------------------------

def test_budget_caps_runs_tokens_cost_and_resets_next_day(store: Store) -> None:
    clock = store.clock
    caps = {"coding_runs_per_day": 3, "tokens_per_day": 1000, "cost_usd_per_day": 1.0, "max_turns": 5, "max_wall_minutes": 1, "local_runs_per_day": 1}
    budget = Budget(store, caps, clock)
    tid, _ = store.add_task("manual", "t")
    assert budget.may_code().ok
    budget.record(tid, "claude", 400, 0.2, 10)
    assert budget.may_code().ok
    budget.record(tid, "claude", 700, 0.2, 10)
    assert "token cap" in budget.may_code().reason
    budget.record(tid, "local:q@gpu", 0, 0, 10)  # local runs never count against cloud caps
    assert budget.store.usage_for(budget.today)["runs"] == 2
    assert not budget.may_code_local().ok
    clock.advance(24 * 3600)
    assert budget.may_code().ok and budget.may_code_local().ok
    assert store.get(tid).coding_runs == 3 and store.get(tid).tokens == 1100


def test_budget_cost_cap(store: Store) -> None:
    budget = Budget(store, {"coding_runs_per_day": 9, "tokens_per_day": 10**9, "cost_usd_per_day": 0.5}, store.clock)
    budget.record(1, "claude", 1, 0.6, 1)
    assert "cost cap" in budget.may_code().reason


def test_backoff_is_exponential_and_capped() -> None:
    assert [backoff_seconds(n, 60, 500) for n in (1, 2, 3, 4, 5)] == [60, 120, 240, 480, 500]


# -- planning ------------------------------------------------------------------------

def _task(store: Store, repo: str = "demo"):
    tid, _ = store.add_task("manual", "Fix the resize flicker", "details", repo)
    return store.get(tid)


def test_normalize_enforces_test_pr_review_ci_and_drops_bad_steps(store: Store) -> None:
    task = _task(store)
    steps = normalize_plan(
        [
            {"kind": "code", "title": "a", "args": {"prompt": "do a"}},
            {"kind": "pr", "title": "model tried to open a PR itself"},
            {"kind": "shell", "title": "rm -rf"},
            {"kind": "game_read", "args": {"tool": "get_player"}},
            {"kind": "game_read", "args": {"tool": "not_listed"}},
            {"kind": "game_action", "args": {"tool": "move_to", "args": {}}},
            {"kind": "code", "title": "b"},
        ],
        task, True, {"get_player"}, set(),
    )
    assert [s["kind"] for s in steps] == ["code", "test", "game_read", "code", "test", "pr", "review", "ci"]
    assert steps[3]["args"]["prompt"].startswith("Fix the resize flicker")


def test_no_code_without_repo(store: Store) -> None:
    task = _task(store, repo="")
    steps = normalize_plan([{"kind": "code", "title": "x"}], task, False, set(), set())
    assert [s["kind"] for s in steps] == ["local"]


def test_planner_falls_back_when_model_is_down_or_rambles(store: Store) -> None:
    task = _task(store)
    for model in (FakeModel([None]), FakeModel(["I think you should..."]), None):
        steps, how = Planner(model).plan(task, True, set(), set())
        assert how.startswith("fallback")
        assert [s["kind"] for s in steps] == ["code", "test", "pr", "review", "ci"]


def test_planner_uses_model_json(store: Store) -> None:
    task = _task(store)
    reply = "```json\n" + json.dumps({"steps": [{"kind": "local", "title": "read the issue"}, {"kind": "code", "title": "fix", "args": {"prompt": "fix it"}}]}) + "\n```"
    steps, how = Planner(FakeModel([reply])).plan(task, True, set(), set())
    assert how == "model"
    assert [s["kind"] for s in steps] == ["local", "code", "test", "pr", "review", "ci"]


def test_replan_after_denial_never_requests_the_same_action(store: Store) -> None:
    task = _task(store, repo="")
    again = json.dumps({"steps": [{"kind": "game_action", "args": {"tool": "reload_plugin", "args": {}}}, {"kind": "local", "title": "tell owner"}]})
    steps, _ = Planner(FakeModel([again])).replan_after_denial(task, "reload_plugin {}", False, set(), {"reload_plugin"})
    assert [s["kind"] for s in steps] == ["local"]


def test_split_and_review_parsing() -> None:
    model = FakeModel([json.dumps({"subtasks": ["part one", "part two", "part three", "four"]}), "garbage"])
    assert split_for_local(model, "big job", 3) == ["part one", "part two", "part three"]
    assert split_for_local(model, "big job", 3) == ["big job"]
    empty_changes = FakeModel([json.dumps({"verdict": "changes", "comments": []})])
    assert review_diff(empty_changes, _FakeTask(), "diff", "")["verdict"] == "approve"


class _FakeTask:
    title = "t"
    body = "b"


# -- sources ---------------------------------------------------------------------------

def test_inbox_items(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    note = tmp_path / "inbox.md"
    note.write_text("# Inbox\n- [ ] fix the resize flicker @demo !high\n- [x] already done\n* [ ] write docs !low\nplain line\n")
    specs = inbox(note, settings.repos)
    assert [(s.title, s.repo) for s in specs] == [("fix the resize flicker", "demo"), ("write docs", "")]
    assert specs[0].value > specs[1].value
    assert specs[0].source_ref == inbox(note, settings.repos)[0].source_ref  # stable across polls


def test_vote_top_ideas_read_only() -> None:
    urls = []

    def get(url: str):
        urls.append(url)
        if url.endswith("tallies"):
            return {"a": {"want": 5, "maybe": 1, "skip": 0}, "b": {"want": 1, "maybe": 0, "skip": 4}, "c": {"want": 2, "maybe": 2, "skip": 0}}
        return {"ideas": [{"id": "a", "title": "Sixel images"}, {"id": "c", "title": "Tabs"}]}

    specs = vote_ideas(get, "https://x/api/tallies", "https://x/ideas.json", 3, "demo")
    assert [s.title for s in specs] == ["Vote idea: Sixel images", "Vote idea: Tabs"]  # b has a negative score
    assert urls == ["https://x/api/tallies", "https://x/ideas.json"]


def test_github_sources_use_read_only_gh(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seen = []

    def run(argv):
        seen.append(argv)
        if argv[1:3] == ["issue", "list"]:
            return 0, json.dumps([{"number": 3, "title": "Crash on resize", "body": "trace", "labels": [{"name": "p1"}], "url": "u"}])
        if argv[1:3] == ["run", "list"]:
            return 0, json.dumps([
                {"databaseId": 9, "displayTitle": "x", "workflowName": "ci", "conclusion": "failure", "url": "u", "headSha": "abc"},
                {"databaseId": 8, "displayTitle": "y", "workflowName": "ci", "conclusion": "success", "url": "u", "headSha": "def"},
                {"databaseId": 7, "displayTitle": "z", "workflowName": "docs", "conclusion": "success", "url": "u", "headSha": "abc"},
            ])
        return 1, "?"

    gh = ReadOnlyGh(run)
    issues = github_issues(gh, settings.repos, "autopilot")
    assert issues[0].source_ref == "github:o/demo#3" and issues[0].value > 45
    ci = failing_ci(gh, settings.repos)
    assert [s.source_ref for s in ci] == ["ci:o/demo:abc:ci"]
    with pytest.raises(GhError):
        gh.json(["pr", "create", "--draft"])
    with pytest.raises(GhError):
        gh.json(["issue", "comment", "3"])
    assert all(a[1:3] in (["issue", "list"], ["run", "list"]) for a in seen)


def test_collect_survives_a_failing_source(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    errors = []
    note = tmp_path / "inbox.md"
    note.write_text("- [ ] one\n")
    cfg = {"github": {"enabled": True}, "inbox": {"enabled": True, "path": str(note)}, "vote": {"enabled": False}, "ci": {"enabled": False}}
    specs = collect(cfg, settings.repos, ReadOnlyGh(lambda argv: (1, "network down")), None, lambda n, e: errors.append(n))
    assert [s.title for s in specs] == ["one"] and errors == ["github"]


# -- coder output and limits ----------------------------------------------------------------

@pytest.mark.parametrize(
    "output,code,kind",
    [
        ('{"is_error":true,"result":"API Error: 429 {\\"type\\":\\"rate_limit_error\\"}"}', 1, "rate_limit"),
        ("Claude AI usage limit reached|1760000000", 1, "usage_limit"),
        ("You've hit your usage limit. Upgrade to Pro or try again later.", 1, "usage_limit"),
        ('{"error":{"type":"insufficient_quota","message":"You exceeded your current quota"}}', 1, "quota"),
        ("Your credit balance is too low to access the Anthropic API.", 1, "quota"),
        ('API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}', 1, "auth"),
        ("Invalid API key · Please run /login", 1, "auth"),
        ("stream error: 429 Too Many Requests", 1, "rate_limit"),
        ("AssertionError in test_foo", 1, ""),
        ("Fixed the rate limit handling in the client.", 0, ""),
    ],
)
def test_classify_limit(output: str, code: int, kind: str) -> None:
    assert classify_limit(ProcResult(code, output, 1.0)) == kind


def test_timeouts_are_not_limits() -> None:
    assert classify_limit(ProcResult(-9, "429 too many requests", 1.0, stopped="timeout")) == ""
    ok = ProcResult(0, "rate_limit_error mentioned in a summary", 1.0)
    assert classify_limit(ok, CodeResult(True, 0, 0, 1, "", "", 1)) == ""


def test_parse_usage() -> None:
    claude = parse_claude(ProcResult(0, json.dumps({"is_error": False, "num_turns": 3, "result": "done", "total_cost_usd": 0.1,
                                                    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 10**6}}), 1))
    assert (claude.ok, claude.tokens, claude.cost, claude.turns) == (True, 15, 0.1, 3)
    events = "\n".join(json.dumps(e) for e in [
        {"type": "thread.started"}, {"type": "item.completed", "item": {"type": "agent_message", "text": "all good"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20}},
    ])
    codex = parse_codex(ProcResult(0, events, 1), usd_per_mtok=10)
    assert (codex.ok, codex.tokens, codex.summary) == (True, 80, "all good") and codex.cost == pytest.approx(0.0008)


def test_local_coder_argv_presets(tmp_path: Path) -> None:
    repo = make_settings(tmp_path).repos["demo"]
    aider = local_coder_argv({"tool": "aider"}, repo, tmp_path, "http://gpu/v1", "qwen-coder", "do it")
    assert aider[:3] == ["aider", "--model", "openai/qwen-coder"] and aider[-1] == "do it"
    assert aider[aider.index("--test-cmd") + 1] == "tests/run.sh"
    codex = local_coder_argv({"tool": "codex"}, repo, tmp_path, "http://gpu/v1", "qwen-coder", "do it")
    assert 'model_providers.autopilot_local.base_url="http://gpu/v1"' in codex and codex[-1] == "do it"
    custom = local_coder_argv({"tool": "custom", "argv": ["my-agent", "--api", "{base_url}", "{prompt}"]}, repo, tmp_path, "http://g/v1", "m", "p")
    assert custom == ["my-agent", "--api", "http://g/v1", "p"]


# -- safety helpers ---------------------------------------------------------------------------

def test_secrets_only_from_references_and_redacted() -> None:
    red = Redactor()
    env = resolve_secrets({"A": "env:SRC", "B": "op://vault/item/field", "C": "env:MISSING"}, {"SRC": "value-from-env"}, red,
                          op_read=lambda ref: "value-from-1password")
    assert env == {"A": "value-from-env", "B": "value-from-1password"}
    assert red("token value-from-1password and value-from-env") == "token *** and ***"
    with pytest.raises(SecretError):
        resolve_secrets({"A": "literal-secret"}, {}, red)


def test_child_env_is_minimal() -> None:
    environ = {"PATH": "/bin", "HOME": "/h", "AWS_SECRET_ACCESS_KEY": "x", "SSH_AUTH_SOCK": "/s", "GH_TOKEN": "y"}
    env = child_env(["PATH", "HOME"], environ, {"ANTHROPIC_API_KEY": "k"}, allow_ssh=False)
    assert set(env) == {"PATH", "HOME", "ANTHROPIC_API_KEY", "GIT_TERMINAL_PROMPT", "ALMANAC_AUTOPILOT"}
    assert "SSH_AUTH_SOCK" in child_env(["PATH"], environ, {}, allow_ssh=True)


def test_bwrap_binds_only_worktree_and_hides_credentials(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    secret_dir = tmp_path / "cfg"
    secret_dir.mkdir()
    argv = bwrap_argv(wt, None, [], allow_ssh=False, allow_network=False, hide=[secret_dir])
    assert argv[argv.index("--ro-bind") + 1: argv.index("--ro-bind") + 3] == ["/", "/"]
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    assert binds == [str(wt)]
    assert "--unshare-net" in argv and ["--tmpfs", str(secret_dir)] == argv[argv.index(str(secret_dir)) - 1: argv.index(str(secret_dir)) + 1]


def test_git_refuses_protected_branches(tmp_path: Path) -> None:
    repo = make_settings(tmp_path).repos["demo"]
    ops = GitOps(lambda *a: ProcResult(0, "", 0), tmp_path, {"main", "master"}, False, lambda m: None)
    for bad in ("main", "master", "feature/x", "autopilot"):
        with pytest.raises(GitError):
            ops.push(repo, tmp_path, bad)
    assert branch_for(12, "Fix: the Resize flicker!!") == "autopilot/12-fix-the-resize-flicker"


def test_game_rules() -> None:
    for tool in ("move_to", "start_combat", "gather_node", "trade_item", "send_chat", "market_buy", "mount_up"):
        assert is_forbidden(tool), tool
    for tool in ("reload_plugin", "equip_gearset", "set_map_flag", "open_window"):
        assert not is_forbidden(tool), tool
    assert ticket_from({"ticket_id": "abc", "state": "PENDING"}).state == "pending"
    nested = ticket_from({"ticket": {"id": "z", "status": "approved", "result": {"ok": True}}})
    assert (nested.id, nested.approved, nested.result) == ("z", True, '{"ok": true}')
