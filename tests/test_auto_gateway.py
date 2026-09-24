"""The gateway's "auto" model and /v1/almanac/gpu, against a fake Ollama and a fake GPU."""

import asyncio
import json
from pathlib import Path

import httpx

from almanac.config import Config, _merge
from almanac.gateway import Gateway, map_model
from almanac.gpu import MB, Gpu
from almanac.guard import Guard
from almanac.residency import Residency

GB = 1024 * MB
TAGS = {"big:9b": int(6.0 * GB), "mid:4b": int(3.0 * GB), "small:2b": int(2.0 * GB)}
AUTO_GATEWAY = {
    "default_model": "auto",
    "allowed_models": [],
    "models": {"claude-*": "auto", "gpt-*": "auto"},
    "auto_models": ["big:9b", "mid:4b", "small:2b"],
}


def auto_config(config: Config, gateway: dict | None = None, guard: dict | None = None) -> Config:
    raw = _merge(config.raw, {"gateway": {**AUTO_GATEWAY, **(gateway or {})}, "guard": guard or {}})
    return Config(raw)


class FakeOllama:
    def __init__(self, loaded: dict[str, int] | None = None) -> None:
        self.loaded = dict(loaded or {})
        self.forwarded: list[str] = []
        self.keep_alive: list[tuple[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n, "size": s} for n, s in TAGS.items()]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": n, "size_vram": s, "expires_at": "2318-01-01T00:00:00Z"}
                                                        for n, s in self.loaded.items()]})
        body = json.loads(request.content)
        if path == "/api/generate":
            self.keep_alive.append((body["model"], body.get("keep_alive")))
            if body.get("keep_alive") == 0:
                self.loaded.pop(body["model"], None)
            return httpx.Response(200, json={})
        self.forwarded.append(body["model"])
        return httpx.Response(200, json={"echo_model": body["model"]})


def gateway(config: Config, free_mb: int | None, ollama: FakeOllama, mem: int = 99999):
    card = Gpu("Quadro P4000", "nvidia", "GPU-p", 8192, 8192 - (free_mb or 0), free_mb or 0, "nvidia-smi")
    gpus = list if free_mb is None else (lambda: [card])
    gw = Gateway(config, guard=Guard(config.section("guard"), mem_reader=lambda: mem, gpu_reader=lambda _: free_mb),
                 client=httpx.AsyncClient(transport=httpx.MockTransport(ollama)), gpus=gpus)
    return gw, httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app()), base_url="http://gw"), config.read_token()


def test_auto_is_allowed_and_mapped() -> None:
    assert map_model("claude-x", AUTO_GATEWAY) == "auto"
    assert map_model("mid:4b", AUTO_GATEWAY) == "mid:4b"  # auto_models count as allowed
    assert map_model("auto", {"allowed_models": ["x"]}) is None  # no auto_models: no auto


def test_requests_follow_free_vram(config: Config) -> None:
    asyncio.run(_follow(auto_config(config)))


async def _follow(cfg: Config) -> None:
    for free, expected in ((8192, "big:9b"), (5818, "mid:4b"), (3000, "small:2b")):
        ollama = FakeOllama()
        _, client, token = gateway(cfg, free, ollama)
        async with client:
            r = await client.post("/v1/messages", json={"model": "claude-x", "messages": []}, headers={"x-api-key": token})
        assert r.status_code == 200 and r.json()["echo_model"] == expected, (free, r.text)


def test_switching_unloads_our_other_model_first(config: Config) -> None:
    asyncio.run(_switch(auto_config(config)))


async def _switch(cfg: Config) -> None:
    # The 9b is resident (6.7 GB) and a game took the rest: only 300 MB free, so the 9b no longer fits even
    # counting its own VRAM back; the 4b is chosen and the 9b is unloaded before the 4b loads.
    ollama = FakeOllama({"big:9b": int(6.7 * GB)})
    _, client, token = gateway(cfg, 300, ollama)  # budget 300 + 6860 - 819 = 6341 MB < the 9b's 6860
    async with client:
        r = await client.post("/v1/chat/completions", json={"model": "gpt-5"}, headers={"authorization": f"Bearer {token}"})
    assert r.json()["echo_model"] in ("mid:4b", "small:2b")
    assert ("big:9b", 0) in ollama.keep_alive and "big:9b" not in ollama.loaded


def test_fixed_vram_floor_does_not_block_auto(config: Config) -> None:
    # [guard] min_gpu_free_mb 6500 would refuse any load at 3000 MB free; auto picks for that and runs.
    cfg = auto_config(config, guard={"gpu_uuid": "GPU-p", "min_gpu_free_mb": 6500})
    asyncio.run(_floor(cfg))


async def _floor(cfg: Config) -> None:
    _, client, token = gateway(cfg, 3000, FakeOllama())
    async with client:
        r = await client.post("/v1/messages", json={"model": "claude-x"}, headers={"x-api-key": token})
        assert r.status_code == 200
        r = await client.post("/v1/messages", json={"model": "big:9b"}, headers={"x-api-key": token})
        assert r.status_code == 503 and "GPU" in r.json()["error"]["message"]  # asking for a model by name still is guarded


def test_unreadable_gpu_still_runs_the_fallback(config: Config) -> None:
    asyncio.run(_fallback(auto_config(config, gateway={"auto_fallback": "mid:4b"})))


async def _fallback(cfg: Config) -> None:
    ollama = FakeOllama()
    _, client, token = gateway(cfg, None, ollama)
    async with client:
        r = await client.post("/v1/messages", json={"model": "claude-x"}, headers={"x-api-key": token})
    assert r.status_code == 200 and r.json()["echo_model"] == "mid:4b"


def test_gpu_report(config: Config) -> None:
    asyncio.run(_report(auto_config(config)))


async def _report(cfg: Config) -> None:
    ollama = FakeOllama({"mid:4b": 3500 * MB})
    _, client, token = gateway(cfg, 2500, ollama)
    async with client:
        assert (await client.get("/v1/almanac/gpu")).status_code == 401
        body = (await client.get("/v1/almanac/gpu", headers={"authorization": f"Bearer {token}"})).json()
        models = (await client.get("/v1/models", headers={"authorization": f"Bearer {token}"})).json()
    assert body["inference"]["name"] == "Quadro P4000" and body["inference"]["free_mb"] == 2500
    assert body["loaded"] == [{"model": "mid:4b", "vram_mb": 3500}] and body["installed"] == sorted(TAGS)
    assert body["reclaimable_mb"] == 3500 and body["headroom_mb"] == 819 and body["budget_mb"] == 2500 + 3500 - 819
    assert body["auto"]["choice"] == "mid:4b" and body["auto"]["models"] == ["big:9b", "mid:4b", "small:2b"]
    assert "auto" in {m["id"] for m in models["data"]}


def test_gpu_report_without_gpu_or_auto(config: Config) -> None:
    async def run() -> None:
        _, client, token = gateway(config, None, FakeOllama())
        async with client:
            body = (await client.get("/v1/almanac/gpu", headers={"x-api-key": token})).json()
        assert body["inference"] is None and "budget_mb" not in body and "auto" not in body
    asyncio.run(run())


def test_residency_follows_auto(tmp_path: Path) -> None:
    asyncio.run(_residency(tmp_path))


async def _residency(tmp_path: Path) -> None:
    ollama = FakeOllama()
    picks = iter(["big:9b", "big:9b", "mid:4b"])

    async def resolve() -> str:
        return next(picks)

    r = Residency({"keep_loaded_while_process": ["ffxiv_dx11.exe"]}, "auto", "http://ollama", httpx.AsyncClient(transport=httpx.MockTransport(ollama)),
                  Guard({}, mem_reader=lambda: 99999), tmp_path, processes=lambda wanted: wanted, resolve=resolve)

    def pin_loads(request_model: str) -> None:  # our fake loads what a keep_alive request names
        ollama.loaded[request_model] = 1 * GB

    await r.tick()
    pin_loads("big:9b")
    assert r.model == "big:9b" and ("big:9b", -1) in ollama.keep_alive and r.state == "pinned"
    await r.tick()  # still the 9b: nothing new
    await r.tick()  # the game grew: the 4b fits best now
    assert r.model == "mid:4b"
    assert ("big:9b", "5m") in ollama.keep_alive  # the 9b was released to its idle keep_alive
    assert ollama.keep_alive[-1] == ("mid:4b", -1)


def test_reservation_steps_down_at_once(config: Config) -> None:
    asyncio.run(_reserve(auto_config(config)))


async def _reserve(cfg: Config) -> None:
    # The 9b is resident on an otherwise quiet card; a game reserves 3500 MB before it allocates anything.
    ollama = FakeOllama({"big:9b": int(6.7 * GB)})
    _, client, token = gateway(cfg, 8192 - 6860, ollama)
    auth = {"authorization": f"Bearer {token}"}
    async with client:
        assert (await client.put("/v1/almanac/gpu/reservations/ffxiv", json={"mb": 3500})).status_code == 401
        assert (await client.put("/v1/almanac/gpu/reservations/ffxiv", json={"mb": "x"}, headers=auth)).status_code == 400
        r = await client.put("/v1/almanac/gpu/reservations/ffxiv", json={"mb": 3500, "ttl_s": 60}, headers=auth)
        assert r.status_code == 200 and r.json()["auto"]["choice"] == "mid:4b"
        assert ("big:9b", 0) in ollama.keep_alive and "big:9b" not in ollama.loaded  # unloaded without waiting for a request
        body = (await client.get("/v1/almanac/gpu", headers=auth)).json()
        assert body["reservations"] == {"ffxiv": 3500}
        r = await client.delete("/v1/almanac/gpu/reservations/ffxiv", headers=auth)
        assert r.json()["released"] is True and r.json()["reservations"] == {}
