import asyncio
import json

import httpx

from almanac.gateway import Gateway, estimate_tokens, map_model
from almanac.guard import Guard

SETTINGS = {"allowed_models": ["small:1"], "models": {"claude-*": "small:1", "big": "huge:70b"}, "default_model": "small:1"}


def test_map_model() -> None:
    assert map_model("claude-sonnet-4-5", SETTINGS) == "small:1"
    assert map_model("small:1", SETTINGS) == "small:1"
    assert map_model("big", SETTINGS) is None  # mapped but not allowed
    assert map_model("unknown", SETTINGS) is None


def test_guard() -> None:
    g = Guard({"min_mem_available_mb": 1000, "unload_below_mem_available_mb": 500, "min_gpu_free_mb": 4000, "gpu_uuid": "GPU-x"},
              mem_reader=lambda: 800, gpu_reader=lambda _: 8000)
    assert not g.may_load(False).ok and g.may_load(True).ok
    g = Guard({"min_mem_available_mb": 100, "min_gpu_free_mb": 4000, "gpu_uuid": "GPU-x"}, mem_reader=lambda: 800, gpu_reader=lambda _: 100)
    assert "GPU" in g.may_load(False).reason
    assert Guard({"unload_below_mem_available_mb": 900}, mem_reader=lambda: 800).should_unload()


def test_estimate_tokens() -> None:
    assert estimate_tokens({"messages": [{"role": "user", "content": "x" * 400}]}) >= 100


def make_client(config, guard_ok=True, loaded=()):
    seen = []

    def backend(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": m} for m in loaded]})
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "echo_model": json.loads(request.content)["model"]})

    mem = 99999 if guard_ok else 10
    gw = Gateway(config, guard=Guard(config.section("guard"), mem_reader=lambda: mem), client=httpx.AsyncClient(transport=httpx.MockTransport(backend)))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app()), base_url="http://gw")
    return client, seen, config.read_token()


def test_gateway_auth_mapping_and_guard(config) -> None:
    asyncio.run(_auth_mapping(config))


async def _auth_mapping(config) -> None:
    client, seen, token = make_client(config)
    async with client:
        assert (await client.post("/v1/messages", json={"model": "claude-x"})).status_code == 401
        r = await client.post("/v1/messages", json={"model": "claude-haiku-4-5", "messages": []}, headers={"x-api-key": token})
        assert r.status_code == 200 and r.json()["echo_model"] == "qwen3.5:9b"
        r = await client.post("/v1/chat/completions", json={"model": "gpt-5"}, headers={"authorization": f"Bearer {token}"})
        assert r.status_code == 200
        r = await client.post("/v1/responses", json={"model": "not-allowed:70b"}, headers={"authorization": f"Bearer {token}"})
        assert r.status_code == 404
        r = await client.post("/v1/messages/count_tokens", json={"messages": [{"content": "hello world"}]}, headers={"x-api-key": token})
        assert r.json()["input_tokens"] >= 1
    assert [p for p, _ in seen] == ["/v1/messages", "/v1/chat/completions"]


def test_gateway_refuses_when_memory_tight(config) -> None:
    asyncio.run(_tight(config))


async def _tight(config) -> None:
    client, seen, token = make_client(config, guard_ok=False)
    async with client:
        r = await client.post("/v1/messages", json={"model": "claude-x"}, headers={"x-api-key": token})
        assert r.status_code == 503 and "memory" in r.json()["error"]["message"]
    client, seen, token = make_client(config, guard_ok=False, loaded=["qwen3.5:9b"])
    async with client:
        r = await client.post("/v1/messages", json={"model": "claude-x"}, headers={"x-api-key": token})
        assert r.status_code == 200  # already resident: no new load
