"""Benchmark runner: scoring vectors, matchers, paths, schema subset and full mock runs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from almanac import bench, cli

REPO = Path(__file__).resolve().parents[1]
SUITE = bench.load_suite(REPO / "benchmark" / "suites" / "ffxiv-core.json")
VECTORS = json.loads((REPO / "benchmark" / "testdata" / "scoring-vectors.json").read_text())


# -- scoring vectors (shared with the C# plugin) ----------------------------


def test_vectors_pin_the_suite() -> None:
    assert VECTORS["suite"]["sha256"] == SUITE.sha256
    assert VECTORS["suite"]["version"] == SUITE.version


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=[v["name"] for v in VECTORS["vectors"]])
def test_scoring_vector(vector: dict[str, Any]) -> None:
    task = SUITE.task(vector["task"])
    offered = SUITE.offered(task)
    calls = []
    for raw in vector["calls"]:
        call = bench.check_call(offered, raw["name"], raw["arguments"])
        if call.valid:
            if vector["mode"] == "mock":  # the vector's result must be what the fixtures return
                assert raw["result"] == bench.fixture_result(SUITE, raw["name"], call.args or {})
            call.result = raw["result"]
        else:
            assert raw["result"] is None
        calls.append(call)
    got = bench.score_task(SUITE, task, vector["mode"], calls, vector["final_answer"])
    want = vector["expected"]
    for key in ("tool_score", "score"):
        assert got.__dict__[key] == pytest.approx(want[key], abs=1e-6), key
    if want["answer_score"] is None:
        assert got.answer_score is None
    else:
        assert got.answer_score == pytest.approx(want["answer_score"], abs=1e-6)
    assert (got.success, got.error, got.valid_calls, got.total_calls) == (want["success"], want["error"], want["valid_calls"], want["total_calls"])


# -- building blocks --------------------------------------------------------


def test_matchers() -> None:
    assert bench.match_value({"eq": 135}, 135.0)
    assert bench.match_value(135, 135)
    assert not bench.match_value({"eq": 1}, True)
    assert not bench.match_value({"eq": "a"}, "A")
    assert bench.match_value({"approx": 11.2, "tolerance": 0.05}, 11.25)
    assert not bench.match_value({"approx": 11.2, "tolerance": 0.05}, "11.2")
    assert bench.match_value({"icontains_any": ["noscea", "x"]}, "Lower LA NOSCEA")
    assert not bench.match_value({"icontains_any": ["noscea"]}, 5)
    assert bench.args_match({}, {"anything": 1})
    assert not bench.args_match({"q": {"eq": 1}}, {})
    assert bench.fixture_result(SUITE, "get_weather_forecast", {"territoryId": 135})["territoryId"] == 135
    assert bench.fixture_result(SUITE, "get_weather_forecast", {})["territoryId"] == 129
    assert bench.fixture_result(SUITE, "no_such_tool", {}) == {"error": "no data"}


def test_paths() -> None:
    doc = {"a": {"b": [{"c": 1}, {"c": 2, "name": "Moraby Drydocks"}]}, "n": None}
    assert bench.resolve_path(doc, "a.b[1].c") == 2
    assert bench.resolve_path(doc, "a.b[name~moraby].c") == 2
    assert bench.resolve_path(doc, "a.b[5].c") is bench.MISSING
    assert bench.resolve_path(doc, "a.b[name~nope].c") is bench.MISSING
    assert bench.resolve_path(doc, "a.x") is bench.MISSING
    assert bench.resolve_path(doc, "n") is None
    fixture = SUITE.raw["fixtures"]["list_aetherytes"][-1]["result"]
    assert bench.resolve_path(fixture, "aetherytes[name~Moraby].gilCost") == 243


def test_exact_and_numbers() -> None:
    assert bench.normalise_exact("```text\n /gearset  change 3 \n```") == "/gearset change 3"
    assert bench.normalise_exact('"/hudlayout 2"') == "/hudlayout 2"
    assert bench.normalise_exact("/hudlayout 2.") == "/hudlayout 2"
    assert bench.answer_numbers("1,234,567 and -3.5 and 9.4, 11.8") == [1234567, -3.5, 9.4, 11.8]


def test_schema_subset() -> None:
    schema = SUITE.tools["post_status"]["inputSchema"]
    assert bench.validate(schema, {"agent": "a", "status": "s", "state": "done", "progress": 0.5}) == []
    assert bench.validate(schema, {"agent": "a"})  # required
    assert bench.validate(schema, {"agent": "a", "status": "s", "extra": 1})  # additionalProperties
    assert bench.validate(schema, {"agent": "a", "status": "s", "state": "nope"})  # enum
    assert bench.validate(schema, {"agent": "a", "status": "s", "progress": 2})  # maximum
    assert bench.validate(schema, {"agent": "x" * 65, "status": "s"})  # maxLength
    assert bench.validate({"type": "integer"}, True)
    assert bench.validate({"type": ["number", "null"]}, None) == []
    assert bench.check_call({}, "x", "{}").invalid == "unknown tool 'x'"
    assert bench.check_call(SUITE.tools, "get_location", "[]").invalid == "arguments are not a JSON object"


def test_prompted_parsing() -> None:
    assert bench.parse_prompted_call('{"tool": "get_location", "arguments": {}}') == ("get_location", "{}")
    assert bench.parse_prompted_call('```json\n{"tool": "search_quests", "arguments": {"query": "x"}}\n```') == ("search_quests", '{"query":"x"}')
    assert bench.parse_prompted_call('{"tool": "get_location"}') == ("get_location", "{}")
    assert bench.parse_prompted_call("You are in Limsa.") is None
    assert bench.parse_prompted_call('{"name": "get_location"}') is None
    system = bench.prompted_system("S", SUITE.offered(SUITE.task("where_am_i")))
    assert system.startswith("S\n\n" + bench.PROMPTED_HEADER + "\nget_location: ")
    assert "Arguments (JSON Schema): {\"type\":\"object\"" in system
    assert bench.prompted_system("S", {}) == "S"


# -- full runs against a fake OpenAI-compatible backend ---------------------

PLAN: dict[str, tuple[list[tuple[str, dict[str, Any]]], str]] = {
    "where_am_i": ([("get_location", {})], "You are in Limsa Lominsa Lower Decks at X 9.4, Y 11.8."),
    "nearest_aetheryte": ([("get_location", {})], "The Limsa Lominsa aetheryte."),
    "weather_next_here": ([("get_weather_forecast", {})], "Clouds."),
    "weather_rain_zone": ([("get_weather_forecast", {"territoryId": 135})], "Yes, Rain at 00:00."),
    "teleport_cost": ([("list_aetherytes", {"nameContains": "Moraby"})], "243 gil."),
    "aetherytes_filtered": ([("list_aetherytes", {"nameContains": "La Noscea"})], "Summerford Farms, Moraby Drydocks, Costa del Sol."),
    "flag_coordinates": ([("set_map_flag", {"x": 11.2, "y": 14.5})], "Flag placed."),
    "quest_level": ([("search_quests", {"query": "It's Probably Pirates"})], "It is level 13."),
    "command_gearset": ([], "```\n/gearset change 3\n```"),
    "command_hudlayout": ([], "`/hudlayout 2`"),
    "restraint_general": ([], "It matches you into a party for a duty."),
    "multi_step_status": (
        [("get_location", {}), ("get_weather_forecast", {}), ("post_status", {"agent": "almanac-bench", "status": "Limsa, clouds next"})],
        "Posted: you are in Limsa Lominsa Lower Decks, clouds next.",
    ),
}
BY_PROMPT = {SUITE.task(tid)["prompt"]: plan for tid, plan in PLAN.items()}


def sse(chunks: list[dict[str, Any]], usage: int | None = None) -> bytes:
    out = [f"data: {json.dumps({'choices': [{'index': 0, 'delta': c}]})}\n\n" for c in chunks]
    if usage is not None:
        out.append(f"data: {json.dumps({'choices': [], 'usage': {'completion_tokens': usage}})}\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


class FakeModel:
    """Plays PLAN: the next planned call, or the final answer once all results are back."""

    def __init__(self, reject_tools: bool = False) -> None:
        self.reject_tools = reject_tools
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        self.requests.append(body)
        assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
        messages = body["messages"]
        if messages[-1]["content"] == bench.WARMUP_PROMPT:
            return httpx.Response(200, content=sse([{"content": "OK"}]))
        if "tools" in body and self.reject_tools:
            return httpx.Response(400, json={"error": f"{body['model']} does not support tools"})
        prompt = next(m["content"] for m in messages if m["role"] == "user")
        calls, answer = BY_PROMPT[prompt]
        prompted = messages[0]["content"].count(bench.PROMPTED_HEADER) == 1
        done = sum(1 for m in messages if m["role"] == "tool" or (m["role"] == "user" and m["content"].startswith("Tool result: ")))
        if done >= len(calls):
            return httpx.Response(200, content=sse([{"reasoning_content": "thinking"}, {"content": answer[:3]}, {"content": answer[3:]}], usage=12))
        name, args = calls[done]
        if prompted:
            text = json.dumps({"tool": name, "arguments": args})
            return httpx.Response(200, content=sse([{"content": f"```json\n{text}\n```" if done else text}]))
        assert "tools" in body and name in {t["function"]["name"] for t in body["tools"]}
        raw = json.dumps(args)
        cut = len(raw) // 2
        return httpx.Response(200, content=sse([
            {"tool_calls": [{"index": 0, "id": f"c{done}", "type": "function", "function": {"name": name, "arguments": raw[:cut]}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": raw[cut:]}}]},
        ]))


def run_fake(model: FakeModel, tool_calling: str = "auto") -> tuple[dict[str, Any], bench.RunOutcome]:
    client = httpx.Client(transport=httpx.MockTransport(model))
    chat = bench.ChatClient("http://backend.test/v1", client=client)
    runner = bench.Runner(SUITE, chat, "fake:1b", bench.mock_executor(SUITE), "mock", tool_calling)
    with bench.VramSampler(lambda: 2048, interval=0.05) as sampler:
        outcome = runner.run()
    hardware = {"gpu_model": "NVIDIA GeForce RTX 4060", "gpu_vendor": "nvidia", "vram_mb": 8192, "system_ram_gb": 32, "os": "linux"}
    doc = bench.build_result(SUITE, "mock", hardware, {"kind": "openai-compatible", "version": None}, {"name": "fake:1b", "quant": "unknown", "context": 0}, outcome, sampler.peak)
    return doc, outcome


def assert_conforms(doc: dict[str, Any]) -> None:
    schema = json.loads(bench.RESULTS_SCHEMA.read_text())
    assert bench.result_errors(doc) == []
    try:
        import jsonschema
    except ImportError:
        return
    jsonschema.validate(doc, schema)


def test_native_run_scores_full_marks() -> None:
    model = FakeModel()
    doc, outcome = run_fake(model)
    assert_conforms(doc)
    assert [t["id"] for t in doc["tasks"]] == [t["id"] for t in SUITE.tasks]
    assert all(t["success"] and t["error"] is None for t in doc["tasks"]), doc["tasks"]
    m = doc["metrics"]
    assert (m["score"], m["success_rate"], m["tool_call_validity"], m["quality"]) == (100.0, 1.0, 1.0, 1.0)
    assert m["peak_vram_mb"] == 2048 and m["ttft_ms"] >= 0 and m["tokens_per_s"] > 0
    assert doc["model"]["tool_calling"] == "native"
    assert doc["suite"] == {"id": "ffxiv-core", "version": SUITE.version, "sha256": SUITE.sha256}
    assert doc["client"]["name"] == "almanac-py"
    # tools only when offered; tool results go back as tool messages with the call id
    first = [r for r in model.requests if r["messages"][-1]["content"] == SUITE.task("command_gearset")["prompt"]][0]
    assert "tools" not in first
    last = model.requests[-1]["messages"]
    assert [m["role"] for m in last[2:]] == ["assistant", "tool"] * 3
    assert last[3]["tool_call_id"] == "c0" and json.loads(last[3]["content"])["territory"]["id"] == 129
    assert {r["temperature"] for r in model.requests[1:]} == {0} and {r["max_tokens"] for r in model.requests[1:]} == {512}
    where = outcome.tasks[0]
    assert where.output_tokens == 12 + 2  # usage on the answer turn, one per delta on the call turn


def test_prompted_fallback_when_backend_rejects_tools() -> None:
    model = FakeModel(reject_tools=True)
    doc, _ = run_fake(model)
    assert_conforms(doc)
    assert doc["model"]["tool_calling"] == "prompted"
    assert doc["metrics"]["score"] == 100.0, doc["tasks"]
    prompted = [r for r in model.requests if "tools" not in r and len(r["messages"]) > 1]
    assert all(r["messages"][0]["content"].startswith(SUITE.raw["system_prompt"]) for r in prompted)
    assert any(m["role"] == "user" and m["content"].startswith("Tool result: {") for r in prompted for m in r["messages"])


def test_model_without_tool_use_reports_none() -> None:
    class Mute(FakeModel):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, content=sse([{"content": "I don't know."}]))

    doc, _ = run_fake(Mute())
    assert_conforms(doc)
    assert doc["model"]["tool_calling"] == "none"
    assert doc["metrics"]["tool_call_validity"] == 1.0


def test_backend_error_is_recorded_per_task() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["messages"][-1]["content"] == bench.WARMUP_PROMPT:
            return httpx.Response(200, content=sse([{"content": "OK"}]))
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner = bench.Runner(SUITE, bench.ChatClient("http://b.test/v1", client=client), "m", bench.mock_executor(SUITE), tool_calling="native")
    run = runner.run_task(SUITE.task("where_am_i"))
    assert run.score.error == "http" and run.score.score == 0


def test_backend_detection_and_ollama_show() -> None:
    def ollama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.3"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={
                "details": {"quantization_level": "Q4_K_M", "family": "qwen3", "parameter_size": "9.7B"},
                "parameters": "temperature 0.6\nnum_ctx 16384",
                "model_info": {"qwen3.context_length": 262144},
            })
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(ollama))
    assert bench.detect_backend(client, "http://x.test/v1") == ("ollama", "0.12.3")
    assert bench.ollama_model_info(client, "http://x.test/v1", "qwen3.5:9b") == {"quant": "Q4_K_M", "family": "qwen3", "params_b": 9.7, "context": 16384}

    def llama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {}, "total_slots": 1})
        return httpx.Response(404)

    assert bench.detect_backend(httpx.Client(transport=httpx.MockTransport(llama)), "http://x.test/v1")[0] == "llamacpp"
    lms = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}) if r.url.path == "/api/v0/models" else httpx.Response(404)))
    assert bench.detect_backend(lms, "http://x.test/v1") == ("lmstudio", None)
    other = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert bench.detect_backend(other, "http://x.test/v1") == ("openai-compatible", None)
    assert bench.gpu_vendor("NVIDIA GeForce RTX 4060") == "nvidia"
    assert bench.gpu_vendor("AMD Radeon RX 7800 XT") == "amd"
    assert bench.gpu_vendor("unknown") == "unknown"


def test_cli_bench_stores_and_submits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    real_client = httpx.Client
    posted: list[dict[str, Any]] = []
    model = FakeModel()
    site = FakeSite()
    monkeypatch.setenv("ALMANAC_LINK_API", "https://link.test/api")
    monkeypatch.setattr(bench.time, "sleep", lambda s: None)
    monkeypatch.setattr(bench, "open_browser", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "board.test":
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            posted.append(json.loads(request.content))
            return httpx.Response(201, json={"ok": True})
        if request.url.host == "link.test":
            return site(request)
        if request.url.path.startswith("/v1/"):
            return model(request)
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(bench, "hardware_facts", lambda: {"gpu_model": "unknown", "gpu_vendor": "unknown", "vram_mb": 0, "os": "linux"})
    monkeypatch.setattr(bench, "vram_reader", lambda kind, url: None)
    monkeypatch.setenv("ALMANAC_LEADERBOARD_URL", "https://board.test/api/results")
    config = tmp_path / "config.toml"
    config.write_text(f'state_dir = "{tmp_path / "state"}"\n')
    out = tmp_path / "result.json"
    code = cli.main(["--config", str(config), "bench", "--mock", "--base-url", "http://backend.test/v1", "--model", "fake:1b",
                     "--tasks", "where_am_i,command_gearset", "--json", str(out), "--submit", "--yes"])
    assert code == 0
    printed = capsys.readouterr().out
    assert "where_am_i" in printed and "stored as run #1" in printed and "submitted run #1" in printed
    assert "BCDF-GHJK" in printed and TOKEN not in printed and DEVICE_CODE not in printed
    doc = json.loads(out.read_text())
    assert_conforms(doc)
    assert posted == [doc]
    assert doc["metrics"]["peak_vram_mb"] is None and doc["metrics"]["score"] == 100.0
    assert str(tmp_path) not in json.dumps(doc)
    rows = sqlite3.connect(tmp_path / "state" / "bench.sqlite").execute("SELECT id, model, submitted, result FROM runs").fetchall()
    assert rows[0][:3] == (1, "fake:1b", 1) and json.loads(rows[0][3]) == doc
    assert cli.main(["--config", str(config), "bench", "--list"]) == 0
    assert "submitted" in capsys.readouterr().out


# -- device link ---------------------------------------------------------------

TOKEN = "gvt_" + "t" * 43
DEVICE_CODE = "dc_" + "d" * 40


def fail(status: int, error: str, message: str = "") -> httpx.Response:
    return httpx.Response(status, json={"ok": False, "error": error, "message": message or f"server says {error}"})


class FakeSite:
    """The sign-in server and the leaderboard: scripted poll answers, then Bearer-gated results."""

    def __init__(self, polls: list[httpx.Response] | None = None, results: list[httpx.Response] | None = None, expires_in: int = 900) -> None:
        self.polls = list(polls or [])
        self.results = list(results or [])
        self.expires_in = expires_in
        self.requests: list[httpx.Request] = []
        self.tokens = 0

    def paths(self) -> list[str]:
        return [r.url.path.rsplit("/api/", 1)[1] for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert TOKEN not in str(request.url) and DEVICE_CODE not in str(request.url)
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/device/code"):
            assert body == {"client_id": "almanac", "scope": "almanac:submit"}
            return httpx.Response(200, json={
                "device_code": DEVICE_CODE, "user_code": "BCDF-GHJK", "verification_uri": "https://link.test/apps",
                "verification_uri_complete": "https://link.test/apps?code=BCDF-GHJK", "expires_in": self.expires_in, "interval": 5,
            })
        if request.url.path.endswith("/device/token"):
            assert body == {"client_id": "almanac", "device_code": DEVICE_CODE}
            if self.polls:
                return self.polls.pop(0)
            self.tokens += 1
            return httpx.Response(200, json={"access_token": TOKEN, "token_type": "Bearer", "expires_in": 15552000, "scope": "almanac:submit", "token_id": "1"})
        if request.url.path.endswith("/token/revoke"):
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/results"):
            return self.results.pop(0) if self.results else httpx.Response(201, json={"ok": True})
        return httpx.Response(404)


class Link:
    def __init__(self, site: FakeSite, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALMANAC_LINK_API", "https://link.test/api")
        self.client = httpx.Client(transport=httpx.MockTransport(site))
        self.said: list[str] = []
        self.slept: list[float] = []
        self.opened: list[str] = []
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self, client: httpx.Client | None = None) -> str:
        return bench.device_link(client or self.client, say=self.said.append, sleep=self.sleep, clock=lambda: self.now, browser=self.opened.append)


def test_link_success_shows_the_code_and_never_the_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    site = FakeSite()
    link = Link(site, monkeypatch)
    assert link() == TOKEN
    said = "\n".join(link.said)
    assert "BCDF-GHJK" in said and "https://link.test/apps" in said
    assert TOKEN not in said and DEVICE_CODE not in said
    assert link.opened == ["https://link.test/apps?code=BCDF-GHJK"] and link.slept == [5.0]


def test_link_pending_then_slow_down_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    site = FakeSite(polls=[fail(400, "authorization_pending"), fail(400, "slow_down"), fail(400, "authorization_pending")])
    link = Link(site, monkeypatch)
    assert link() == TOKEN
    assert link.slept == [5.0, 5.0, 10.0, 10.0]


@pytest.mark.parametrize("error", ["access_denied", "expired_token", "invalid_grant"])
def test_link_stops_with_the_server_message(monkeypatch: pytest.MonkeyPatch, error: str) -> None:
    site = FakeSite(polls=[fail(400, "authorization_pending"), fail(400, error, f"no: {error}")])
    link = Link(site, monkeypatch)
    with pytest.raises(bench.LinkError, match=f"no: {error}"):
        link()
    assert site.paths() == ["device/code", "device/token", "device/token"]


def test_link_gives_up_at_expires_in(monkeypatch: pytest.MonkeyPatch) -> None:
    site = FakeSite(polls=[fail(400, "authorization_pending")] * 50, expires_in=20)
    link = Link(site, monkeypatch)
    with pytest.raises(bench.LinkError, match="expired"):
        link()
    assert sum(link.slept) < 20 and site.tokens == 0


def test_link_refused_at_the_code_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    link = Link(FakeSite(), monkeypatch)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: fail(429, "busy", "try again in an hour")))
    with pytest.raises(bench.LinkError, match="try again in an hour"):
        link(client)


def test_open_browser_is_harmless_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    import webbrowser

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(bench.platform, "system", lambda: "Linux")
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("opened a browser without a display"))
    bench.open_browser("https://link.test/apps")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("no browser")))
    bench.open_browser("https://link.test/apps")


def test_token_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "state" / bench.LINK_TOKEN_FILE
    bench.write_link_token(path, "old")
    path.chmod(0o644)
    bench.write_link_token(path, TOKEN)
    assert path.stat().st_mode & 0o777 == 0o600 and bench.read_link_token(path) == TOKEN


def test_submit_links_once_then_reuses_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = FakeSite()
    link = Link(site, monkeypatch)
    path = tmp_path / bench.LINK_TOKEN_FILE
    for _ in range(2):
        assert bench.submit_linked({"a": 1}, path, link.client, "https://link.test/api/results", link=link).status_code == 201
    assert site.paths() == ["device/code", "device/token", "results", "results"]
    assert [r.headers["Authorization"] for r in site.requests[2:]] == [f"Bearer {TOKEN}"] * 2
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("error", sorted(bench.RELINK_ERRORS))
def test_refused_token_is_dropped_and_linked_again_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str) -> None:
    path = tmp_path / bench.LINK_TOKEN_FILE
    bench.write_link_token(path, "gvt_stale")
    site = FakeSite(results=[fail(401, error)])
    link = Link(site, monkeypatch)
    assert bench.submit_linked({"a": 1}, path, link.client, "https://link.test/api/results", link=link).status_code == 201
    assert site.paths() == ["results", "device/code", "device/token", "results"]
    assert bench.read_link_token(path) == TOKEN

    site = FakeSite(results=[fail(401, error), fail(401, error, "still no")])
    link = Link(site, monkeypatch)
    response = bench.submit_linked({"a": 1}, path, link.client, "https://link.test/api/results", link=link)
    assert response.status_code == 401 and bench.server_message(response)[1] == "still no"
    assert site.paths() == ["results", "device/code", "device/token", "results"] and not path.exists()


def test_forbidden_is_not_retried_and_keeps_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / bench.LINK_TOKEN_FILE
    bench.write_link_token(path, TOKEN)
    site = FakeSite(results=[fail(403, "account_banned", "this account may not submit")])
    link = Link(site, monkeypatch)
    response = bench.submit_linked({"a": 1}, path, link.client, "https://link.test/api/results", link=link)
    assert response.status_code == 403 and site.paths() == ["results"] and path.exists()
    assert bench.server_message(response) == ("account_banned", "this account may not submit")


def test_cli_shows_the_server_message_and_unlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    doc, _ = run_fake(FakeModel())
    real_client = httpx.Client
    site = FakeSite(results=[fail(403, "account_banned", "this account may not submit")])
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(transport=httpx.MockTransport(site)))
    monkeypatch.setenv("ALMANAC_LEADERBOARD_URL", "https://link.test/api/results")
    monkeypatch.setenv("ALMANAC_LINK_API", "https://link.test/api")
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(f'state_dir = "{state}"\n')
    state.mkdir()
    bench.write_link_token(state / bench.LINK_TOKEN_FILE, TOKEN)
    bench.Store(state / "bench.sqlite").add(doc)

    assert cli.main(["--config", str(config), "bench", "--run", "1", "--submit", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "this account may not submit" in captured.err and TOKEN not in captured.out + captured.err

    assert cli.main(["--config", str(config), "bench", "--unlink"]) == 0
    assert "signed out" in capsys.readouterr().out and not (state / bench.LINK_TOKEN_FILE).exists()
    assert site.requests[-1].url.path.endswith("/token/revoke") and site.requests[-1].headers["Authorization"] == f"Bearer {TOKEN}"
    assert cli.main(["--config", str(config), "bench", "--unlink"]) == 0
    assert "not signed in" in capsys.readouterr().out
