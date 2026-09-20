"""`almanac local` / `ai`: backend discovery, probing, selection, arguments, sessions. No network."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from almanac import local
from almanac.autopilot.coder import LOCAL_PRESETS
from almanac.cli import main as almanac_main
from almanac.config import DEFAULTS, Config, _merge
from almanac.local import LocalBackend, Probe

from test_guard_gateway import make_client


def cfg_with(tmp_path: Path, extra: dict[str, Any]) -> Config:
    return Config(_merge(DEFAULTS, {"state_dir": str(tmp_path / "state"), "token_file": str(tmp_path / "token"), **extra}))


class FakeResponse:
    def __init__(self, status_code: int = 200, data: Any = None) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> Any:
        if self._data is None:
            raise ValueError("not json")
        return self._data


def fake_get(replies: dict[str, Any]):
    def get(url: str, **_: Any) -> FakeResponse:
        for prefix, reply in replies.items():
            if url.startswith(prefix):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise httpx.ConnectError("no route")

    return get


class Clock:
    def __init__(self, step: float) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


# -- config ---------------------------------------------------------------------
def test_backends_from_local_section(tmp_path: Path) -> None:
    (tmp_path / "box.token").write_text("s3cret\n")
    cfg = cfg_with(tmp_path, {"local": {"backends": {
        "box": {"url": "http://192.0.2.10:41881/", "gpu": "Example GPU 8 GB", "context": 16384, "expected_tok_s": 19,
                "token_file": str(tmp_path / "box.token"), "priority": 20, "unknown_key": 1},
        "off": {"url": "http://192.0.2.11:41881", "enabled": False},
    }}, "autopilot": {"pool": {"ignored": {"url": "http://192.0.2.12:41881"}}}})
    [box] = local.load_backends(cfg)
    assert (box.name, box.base_url, box.context, box.gpu, box.priority) == ("box", "http://192.0.2.10:41881", 16384, "Example GPU 8 GB", 20)
    assert box.headers() == {"Authorization": "Bearer s3cret"}


def test_backends_fall_back_to_the_autopilot_pool_then_the_gateway(tmp_path: Path) -> None:
    pool = {"gpu-box": {"url": "http://192.0.2.20:41881", "kind": "almanac", "model": "m:1b", "coder_model": "coder:7b",
                        "roles": ["coder"], "slots": 1, "priority": 5, "timeout": 900}}
    [b] = local.load_backends(cfg_with(tmp_path, {"autopilot": {"pool": pool}}))
    assert (b.name, b.model, b.priority) == ("gpu-box", "coder:7b", 5)
    [b] = local.load_backends(cfg_with(tmp_path, {"gateway": {"listen": ["127.0.0.1:41999"]}}))
    assert (b.name, b.url, b.kind, b.model, b.context) == ("this-machine", "http://127.0.0.1:41999", "almanac", "qwen3.5:9b", 8192)
    assert b.token_file == str(tmp_path / "token")


def test_token_from_env_and_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOX_TOKEN", "abc")
    assert LocalBackend("a", "http://x", token_env="BOX_TOKEN").token() == "abc"
    assert LocalBackend("a", "http://x", token_file="/nonexistent/token").headers() == {}


def test_example_config_declares_only_placeholder_backends() -> None:
    import tomllib

    text = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text()
    block = text[text.index("# [local]"):text.index("# Companion MCP servers")]
    uncommented = "\n".join(line[2:] for line in block.splitlines() if line.startswith("# "))
    section = tomllib.loads(uncommented)["local"]
    [backend] = [LocalBackend.from_config(n, r) for n, r in section["backends"].items()]
    assert backend.url.startswith("http://100.64.0.") and "example" in section["webui_url"]


# -- probing ----------------------------------------------------------------------
def test_health_up_down_and_token_hint() -> None:
    up = LocalBackend("up", "http://up")
    get = fake_get({"http://up/healthz": FakeResponse(200, {"ok": True, "loaded": ["qwen3.5:9b"], "inflight": 2}),
                    "http://denied/v1/models": FakeResponse(401, {}),
                    "http://old/healthz": FakeResponse(200, {"ok": True, "loaded": []}),
                    "http://ollama/api/version": FakeResponse(200, {"version": "0.34"})})
    probe = local.check_health(up, get)
    assert probe.up and probe.loaded == ["qwen3.5:9b"] and probe.inflight == 2 and probe.busy and "2 requests in flight" in probe.state
    assert "token" in local.check_health(LocalBackend("d", "http://denied", kind="openai"), get).reason
    down = local.check_health(LocalBackend("x", "http://gone"), get)
    assert not down.up and "unreachable" in down.state
    old = local.check_health(LocalBackend("o", "http://old"), get)  # a gateway without the inflight field
    assert old.up and old.inflight is None and not old.busy and "not loaded" in old.state
    assert local.check_health(LocalBackend("l", "http://ollama", kind="ollama"), get).state == "free"


def test_speed_probe_counts_tokens_between_first_and_last() -> None:
    probe = Probe(LocalBackend("a", "http://a"), up=True)
    sent: list[dict[str, Any]] = []

    def stream(backend: LocalBackend, payload: dict[str, Any], timeout: float):
        sent.append(payload)
        yield from ["1", " 2", " 3", " 4", " 5"]

    # clock ticks: start=1, then one tick per token (2..6): 4 tokens in 4 s after the first
    local.measure_speed(probe, stream, clock=Clock(1.0))
    assert probe.tok_s == pytest.approx(1.0) and probe.ttft_s == pytest.approx(1.0) and not probe.busy
    assert sent[0]["max_tokens"] <= 64 and sent[0]["reasoning_effort"] == "none"

    def with_usage(*_: Any):
        yield from ["1 2", " 3 4", {"completion_tokens": 30}]

    # the server's token count wins over the number of deltas: 30 tokens, start=1 .. last delta=3
    assert local.measure_speed(Probe(LocalBackend("a", "http://a"), up=True), with_usage, clock=Clock(1.0)).tok_s == pytest.approx(15.0)


def test_speed_probe_timeout_means_busy_and_errors_do_not() -> None:
    def slow(*_: Any):
        raise httpx.ReadTimeout("slow")
        yield ""

    def broken(*_: Any):
        raise httpx.ConnectError("HTTP 401: nope")
        yield ""

    busy = local.measure_speed(Probe(LocalBackend("a", "http://a"), up=True), slow, timeout=7)
    assert busy.busy and "loading the model (timed out after 7s)" in busy.state
    busy.loaded = ["m"]
    assert busy.state == "busy? (timed out after 7s)"
    failed = local.measure_speed(Probe(LocalBackend("a", "http://a"), up=True), broken)
    assert not failed.busy and "probe failed" in failed.state and failed.tok_s is None
    assert local.measure_speed(Probe(LocalBackend("a", "http://a"), up=True), lambda *_: iter(())).probe_error == "no tokens came back"


def test_discover_probes_only_backends_that_are_up_and_idle() -> None:
    backends = [LocalBackend("up", "http://up"), LocalBackend("busy", "http://busy"), LocalBackend("down", "http://down")]
    get = fake_get({"http://up/": FakeResponse(200, {"loaded": ["m"], "inflight": 0}), "http://busy/": FakeResponse(200, {"inflight": 1})})
    probed: list[str] = []

    def stream(backend: LocalBackend, *_: Any):
        probed.append(backend.name)
        return iter(["a", "b", "c"])

    probes = local.discover(backends, get=get, stream=stream)
    assert [p.backend.name for p in probes] == ["up", "busy", "down"] and probed == ["up"]
    assert local.discover(backends, speed=False, get=get, stream=stream)[0].tok_s is None and probed == ["up"]
    assert local.discover([]) == []


# -- selection ----------------------------------------------------------------------
def _probe(name: str, up: bool = True, priority: int = 0, inflight: int = 0, tok_s: float | None = None, expected: float = 0.0) -> Probe:
    return Probe(LocalBackend(name, f"http://{name}", priority=priority, expected_tok_s=expected), up=up, inflight=inflight, tok_s=tok_s)


def test_choose_prefers_free_then_priority_then_speed() -> None:
    assert local.choose([_probe("a", priority=1), _probe("b", priority=9)]).backend.name == "b"  # type: ignore[union-attr]
    assert local.choose([_probe("a", priority=1), _probe("b", priority=9, inflight=1)]).backend.name == "a"  # type: ignore[union-attr]
    assert local.choose([_probe("a", tok_s=20), _probe("b", expected=40)]).backend.name == "b"  # type: ignore[union-attr]
    assert local.choose([_probe("a", priority=1, inflight=1), _probe("b", up=False, priority=9)]).backend.name == "a"  # type: ignore[union-attr]
    assert local.choose([_probe("a", up=False)]) is None and local.choose([]) is None


def test_choose_honours_a_pinned_backend_only_while_it_is_up() -> None:
    probes = [_probe("a", priority=9), _probe("b", priority=1)]
    assert local.choose(probes, "b").backend.name == "b"  # type: ignore[union-attr]
    probes[1].up = False
    assert local.choose(probes, "b").backend.name == "a"  # type: ignore[union-attr]


def test_use_pins_and_unpins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = cfg_with(tmp_path, {"local": {"default": "b", "backends": {"a": {"url": "http://a"}, "b": {"url": "http://b"}}}})
    assert local.preferred_name(cfg) == "b" and local.preferred_name(cfg, "a") == "a"
    assert local.run_use(cfg, argparse.Namespace(name="a")) == 0 and local.preferred_name(cfg) == "a"
    assert local.run_use(cfg, argparse.Namespace(name="nope")) == 2 and local.preferred_name(cfg) == "a"
    assert local.run_use(cfg, argparse.Namespace(name="auto")) == 0 and local.preferred_name(cfg) == "b"
    assert local.run_use(cfg, argparse.Namespace(name=None)) == 0 and "backends: a, b" in capsys.readouterr().out


# -- what is shown ----------------------------------------------------------------------
def test_status_table_and_banner_state_the_limits() -> None:
    fast = Probe(LocalBackend("box", "http://box", gpu="Example GPU", context=32768, expected_tok_s=19), up=True, loaded=["m"], tok_s=18.6)
    down = Probe(LocalBackend("other", "http://other"), reason="unreachable (ConnectError)")
    table = local.status_table([fast, down], fast)
    assert "BACKEND" in table and "32k" in table and "19 tok/s now (usual ~19)" in table and "DOWN: unreachable" in table
    assert [line.startswith("*") for line in table.splitlines()] == [False, True, False]
    text = local.banner(fast)
    assert "32k tokens" in text and "19 tok/s" in text and "weak at" in text and "large refactors" in text


# -- sessions -----------------------------------------------------------------------------
def test_codex_argv_is_the_autopilot_preset_made_interactive(tmp_path: Path) -> None:
    backend = LocalBackend("box", "http://192.0.2.10:41881/", model="coder:7b", context=16384)
    preset = LOCAL_PRESETS["codex"]
    argv = local.codex_argv(backend, tmp_path, "", interactive=True, extra=["--search"])
    assert argv[0] == "codex" and "exec" not in argv and "--json" not in argv and argv[-1] == "--search"
    assert not any("{" in part for part in argv)
    # every hardening option of the preset survives, in order
    kept = [part for part in preset if part not in ("exec", "--json", "{prompt}")]
    assert len(argv) - 1 == len(kept)
    for option in ('web_search="disabled"', "model_context_window=16384", "model_auto_compact_token_limit=10922",
                   'model_providers.autopilot_local.base_url="http://192.0.2.10:41881/v1"', 'approval_policy="never"', "workspace-write"):
        assert option in argv
    assert argv[argv.index("-m") + 1] == "coder:7b" and argv[argv.index("-C") + 1] == str(tmp_path)

    one_shot = local.codex_argv(backend, tmp_path, "fix the test", interactive=False, extra=["--color", "never"])
    assert one_shot[:3] == ["codex", "exec", "--skip-git-repo-check"] and one_shot[-3:] == ["--color", "never", "fix the test"]
    (tmp_path / ".git").mkdir()
    assert "--skip-git-repo-check" not in local.codex_argv(backend, tmp_path, "x", interactive=False)


def _session_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, up: bool = True) -> Config:
    (tmp_path / "box.token").write_text("s3cret\n")
    cfg = cfg_with(tmp_path, {"local": {"backends": {"box": {"url": "http://box", "token_file": str(tmp_path / "box.token")}}}})
    reply = {"http://box/": FakeResponse(200, {"loaded": ["m"]})} if up else {}
    monkeypatch.setattr(local.httpx, "get", fake_get(reply))
    monkeypatch.setattr(local.shutil, "which", lambda name: f"/usr/bin/{name}")
    return cfg


def test_code_launches_codex_with_an_isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _session_cfg(tmp_path, monkeypatch)
    launched: list[Any] = []
    ns = argparse.Namespace(path=str(tmp_path), prompt="", extra=[], backend="", tool="codex")
    assert local.run_code(cfg, ns, launch=lambda argv, env, cwd: launched.append((argv, env, cwd)) or 0) == 0
    argv, env, cwd = launched[0]
    assert argv[0] == "codex" and cwd == tmp_path.resolve()
    assert env["CODEX_HOME"] == str(cfg.state_dir / "local" / "codex-home") and env["AUTOPILOT_LOCAL_KEY"] == "s3cret"
    shown = capsys.readouterr()
    assert "weak at" in shown.err and "32k tokens" in shown.err and "s3cret" not in shown.err + shown.out


def test_sessions_say_so_when_nothing_is_reachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _session_cfg(tmp_path, monkeypatch, up=False)
    ns = argparse.Namespace(path=str(tmp_path), prompt="", extra=[], backend="")
    assert local.run_code(cfg, ns, launch=lambda *_: pytest.fail("must not launch")) == 1
    assert "no local backend is reachable" in capsys.readouterr().err
    assert local.run_code(cfg, argparse.Namespace(path=str(tmp_path / "missing"), prompt="", extra=[], backend=""), launch=lambda *_: 0) == 2


def test_ask_streams_the_answer_and_appends_piped_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import io

    cfg = _session_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("def f(): pass\n"))
    asked: list[dict[str, Any]] = []

    def stream(backend: LocalBackend, payload: dict[str, Any], timeout: float):
        asked.append(payload)
        return iter(["It does ", "nothing.", {"completion_tokens": 4}])

    assert local.run_ask(cfg, argparse.Namespace(question=["explain"], quiet=False, backend=""), stream) == 0
    out = capsys.readouterr()
    assert out.out == "It does nothing.\n" and "[box: qwen3.5:9b, 32k context" in out.err  # banner on stderr, answer alone on stdout
    assert asked[0]["messages"][-1]["content"].startswith("explain\n\n```\ndef f(): pass")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert local.run_ask(cfg, argparse.Namespace(question=[], quiet=True, backend=""), stream) == 2


def _launch_into(launched: list[Any]) -> Any:
    return lambda argv, env, cwd: launched.append((argv, env, cwd)) or 0


def test_code_defaults_to_claude_bare_with_an_isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _session_cfg(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "cloud-session")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cloud-oauth")
    launched: list[Any] = []
    ns = argparse.Namespace(path=str(tmp_path), prompt="", extra=["--verbose"], backend="")  # no --tool, no [local] coder
    assert local.run_code(cfg, ns, launch=_launch_into(launched)) == 0
    argv, env, cwd = launched[0]
    assert argv == ["claude", "--bare", "--verbose"] and cwd == tmp_path.resolve()
    assert env["ANTHROPIC_BASE_URL"] == "http://box" and env["ANTHROPIC_API_KEY"] == "s3cret" and env["MAX_THINKING_TOKENS"] == "0"
    assert "ANTHROPIC_AUTH_TOKEN" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(cfg.state_dir / "local" / "claude-config") and "CODEX_HOME" not in env
    shown = capsys.readouterr()
    for honest in ("qwen3.5:9b", "32k tokens", "weak at", "no plugins, MCP servers, hooks, CLAUDE.md", "--tool codex"):
        assert honest in shown.err
    assert "s3cret" not in shown.err + shown.out


def test_one_shot_prompt_reaches_both_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _session_cfg(tmp_path, monkeypatch)
    launched: list[Any] = []
    for tool in ("claude", "codex"):
        ns = argparse.Namespace(path=str(tmp_path), prompt="fix the test", extra=["--max-turns", "9"] if tool == "claude" else [], backend="", tool=tool)
        assert local.run_code(cfg, ns, launch=_launch_into(launched)) == 0
    claude, codex = launched[0][0], launched[1][0]
    assert claude[:4] == ["claude", "--bare", "--max-turns", "9"] and claude[claude.index("-p") + 1] == "fix the test"
    assert claude[claude.index("--permission-mode") + 1] == "acceptEdits" and claude[claude.index("--allowedTools") + 1] == "Bash,Edit,Read,Write"
    denied = claude[claude.index("--disallowedTools") + 1]
    assert "Bash(sudo:*)" in denied and "Bash(git push:*)" in denied and "Bash(ssh:*)" in denied
    assert codex[:2] == ["codex", "exec"] and codex[-1] == "fix the test"
    assert "-p" not in local.claude_argv() and local.claude_argv(extra=["-c"]) == ["claude", "--bare", "-c"]


def test_tool_comes_from_the_flag_then_the_config_then_claude(tmp_path: Path) -> None:
    everything = lambda name: f"/usr/bin/{name}"
    assert local.pick_coder(cfg_with(tmp_path, {}), which=everything) == ("claude", "")
    codex_cfg = cfg_with(tmp_path, {"local": {"coder": "Codex"}})
    assert local.pick_coder(codex_cfg, which=everything) == ("codex", "")
    assert local.pick_coder(codex_cfg, "claude", which=everything) == ("claude", "")  # the flag wins
    tool, notice = local.pick_coder(cfg_with(tmp_path, {"local": {"coder": "aider"}}), which=everything)
    assert tool == "" and "unknown coder 'aider'" in notice and "claude, codex" in notice


def test_missing_tool_falls_back_to_the_other_with_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    only = lambda have: (lambda name: f"/usr/bin/{name}" if name == have else None)
    cfg = cfg_with(tmp_path, {})
    tool, notice = local.pick_coder(cfg, which=only("codex"))
    assert tool == "codex" and notice.count("\n") == 0 and "`claude` is not installed" in notice and "using codex" in notice
    tool, notice = local.pick_coder(cfg, "codex", which=only("claude"))
    assert tool == "claude" and "`codex` is not installed" in notice and "using claude" in notice
    tool, notice = local.pick_coder(cfg, which=lambda _: None)
    assert tool == "" and "@anthropic-ai/claude-code" in notice and "@openai/codex" in notice and "ai chat" in notice

    cfg = _session_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(local.shutil, "which", only("codex"))
    launched: list[Any] = []
    assert local.run_code(cfg, argparse.Namespace(path=str(tmp_path), prompt="", extra=[], backend="", tool=""), launch=_launch_into(launched)) == 0
    assert launched[0][0][0] == "codex" and "using codex instead" in capsys.readouterr().err
    monkeypatch.setattr(local.shutil, "which", lambda _: None)
    assert local.run_code(cfg, argparse.Namespace(path=str(tmp_path), prompt="", extra=[], backend="", tool=""), launch=lambda *_: pytest.fail("must not launch")) == 1
    cfg = cfg_with(tmp_path, {"local": {"coder": "vim"}})
    assert local.run_code(cfg, argparse.Namespace(path=str(tmp_path), prompt="", extra=[], backend="", tool=""), launch=lambda *_: pytest.fail("must not launch")) == 2


def test_claude_needs_a_backend_that_serves_the_messages_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = cfg_with(tmp_path, {"local": {"backends": {"vllm": {"url": "http://vllm", "kind": "openai"}}}})
    monkeypatch.setattr(local.httpx, "get", fake_get({"http://vllm/": FakeResponse(200, {"data": []})}))
    monkeypatch.setattr(local.shutil, "which", lambda name: f"/usr/bin/{name}")
    ns = argparse.Namespace(path=str(tmp_path), prompt="", extra=[], backend="", tool="claude")
    assert local.run_code(cfg, ns, launch=lambda *_: pytest.fail("must not launch")) == 1
    assert "--tool codex" in capsys.readouterr().err
    launched: list[Any] = []
    ns.tool = "codex"
    assert local.run_code(cfg, ns, launch=_launch_into(launched)) == 0 and launched[0][0][0] == "codex"


def test_ai_claude_is_an_alias_and_the_old_flag_still_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _session_cfg(tmp_path, monkeypatch)
    cfg.raw["local"]["coder"] = "codex"  # the alias means claude whatever the config says
    launched: list[Any] = []
    for argv in (["claude", str(tmp_path)], ["claude", str(tmp_path), "--experimental", "-p", "fix it"]):
        ns = parse(*argv)
        assert ns.local_func is local.run_claude
        assert local.run_claude(cfg, ns, launch=_launch_into(launched)) == 0
    assert launched[0][0] == ["claude", "--bare"] and launched[1][0][:4] == ["claude", "--bare", "-p", "fix it"]


def test_no_subcommand_in_a_repo_starts_the_default_coder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _session_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(local.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(local.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **k: argparse.Namespace(returncode=0))
    monkeypatch.setattr(local, "run_status", lambda cfg, ns: 0)
    started: list[str] = []
    monkeypatch.setattr(local, "run_code", lambda cfg, ns: started.append(f"code:{ns.tool or 'default'}") or 0)
    monkeypatch.setattr(local, "run_chat", lambda cfg, ns: started.append("chat") or 0)
    assert local.run_default(cfg, parse()) == 0
    monkeypatch.setattr(local.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    assert local.run_default(cfg, parse()) == 0  # claude alone is enough
    monkeypatch.setattr(local.shutil, "which", lambda _: None)
    assert local.run_default(cfg, parse()) == 0
    assert started == ["code:default", "code:default", "chat"]


# -- arguments -------------------------------------------------------------------------------
def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    local.add_arguments(parser)
    return parser.parse_args(list(argv))


def test_argument_handling() -> None:
    assert parse().local_func is local.run_default
    ns = parse("code")
    assert (ns.local_func, ns.path, ns.prompt, ns.extra, ns.backend, ns.tool) == (local.run_code, ".", "", [], "", "")
    ns = parse("code", "~/src/x", "-p", "fix it", "-b", "box", "--tool", "codex", "--", "--search")
    assert (ns.path, ns.prompt, ns.backend, ns.tool, ns.extra) == ("~/src/x", "fix it", "box", "codex", ["--search"])
    with pytest.raises(SystemExit):
        parse("code", "--tool", "aider")
    ns = parse("ask", "-q", "why", "is", "it", "red")
    assert ns.local_func is local.run_ask and ns.question == ["why", "is", "it", "red"] and ns.quiet
    assert parse("status", "--no-probe", "--json").no_probe and parse("use").name is None and parse("use", "auto").name == "auto"
    assert parse("chat", "--backend", "box").backend == "box" and parse("claude").tool == ""
    with pytest.raises(SystemExit):
        parse("frobnicate")


def test_both_entry_points_reach_the_same_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'state_dir = "{tmp_path}/state"\n[local]\nwebui_url = "https://chat.example.ts.net"\n[local.backends.box]\nurl = "http://box"\ngpu = "Example GPU"\n')
    monkeypatch.setattr(local.httpx, "get", fake_get({"http://box/": FakeResponse(200, {"loaded": ["m"], "inflight": 0})}))
    assert almanac_main(["--config", str(config), "local", "status", "--no-probe"]) == 0
    first = capsys.readouterr().out
    assert local.main(["--config", str(config), "status", "--no-probe"]) == 0
    assert first == capsys.readouterr().out and "Example GPU" in first and "https://chat.example.ts.net" in first
    assert local.main(["--config", str(config), "status", "--no-probe", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["chosen"] is True
    assert local.main(["--config", str(config), "webui"]) == 0 and local.main(["--config", str(config), "limits"]) == 0
    with pytest.raises(SystemExit) as stop:
        local.main(["--help"])
    assert stop.value.code == 0 and "Claude ran out?" in capsys.readouterr().out


# -- gateway: the inflight counter status relies on --------------------------------------------
def test_gateway_reports_inflight(config: Config) -> None:
    async def run() -> None:
        client, _, token = make_client(config)
        async with client:
            assert (await client.get("/healthz")).json()["inflight"] == 0
            r = await client.post("/v1/chat/completions", json={"model": "gpt-5"}, headers={"authorization": f"Bearer {token}"})
            assert r.status_code == 200
            assert (await client.get("/healthz")).json()["inflight"] == 0  # back to zero once the reply is relayed

    asyncio.run(run())
