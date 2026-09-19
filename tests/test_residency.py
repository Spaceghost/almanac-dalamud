import asyncio
import json
import logging
from pathlib import Path

import httpx

from almanac.gateway import Gateway
from almanac.guard import Guard
from almanac.residency import Residency, is_pinned, process_names, read_state, running_matches
from almanac.service import Almanac

FOREVER = "2318-08-17T02:40:07.123456789-07:00"
SOON = "2026-09-19T12:05:00.123456789-07:00"


def fake_proc(root: Path, procs: dict[int, list[str]]) -> Path:
    root.mkdir(exist_ok=True)
    (root / "self").mkdir(exist_ok=True)
    (root / "meminfo").write_text("MemAvailable: 1 kB\n")
    for pid, argv in procs.items():
        (root / str(pid)).mkdir()
        (root / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (root / "999").mkdir()  # exited between listing and reading: no cmdline
    return root


def test_process_names() -> None:
    assert process_names(["/usr/bin/foo", "-x"]) == ["foo"]
    assert process_names([r"Z:\home\u\Games\FINAL FANTASY XIV Online\game\ffxiv_dx11.exe", "//**sqex**//"]) == ["ffxiv_dx11.exe"]
    assert process_names(["/opt/wine/bin/wine64-preloader", r"C:\Game\FFXIV_DX11.EXE"]) == ["wine64-preloader", "ffxiv_dx11.exe"]
    assert process_names([""]) == [] and process_names([]) == []


def test_running_matches(tmp_path: Path) -> None:
    root = fake_proc(tmp_path / "proc", {
        10: [r"Z:\home\u\game\FFXIV_DX11.exe", "arg"],
        11: ["/usr/bin/bash", "-c", "pgrep -f ffxiv_dx11.exe"],   # mentioned, not running
        12: ["/usr/bin/vim", "notes/ffxiv_dx11.exe"],
        13: ["wine", "C:\\x\\other.exe"],
        14: ["/usr/lib/steam/steam"],
    })
    wanted = ["ffxiv_dx11.exe", "steam", "missing.exe"]
    assert running_matches(wanted, root) == ["ffxiv_dx11.exe", "steam"]
    assert running_matches(["other.exe"], root) == ["other.exe"]  # argv[1] under a Wine loader
    assert running_matches(["bash"], root) == ["bash"]
    assert running_matches([], root) == []
    assert running_matches(["ffxiv_dx11.exe"], tmp_path / "nope") == []


def test_is_pinned() -> None:
    assert is_pinned(FOREVER) and not is_pinned(SOON) and not is_pinned("") and not is_pinned("garbage")


class FakeOllama:
    """/api/ps + /api/generate with keep_alive, like Ollama; ``up`` False = connection refused."""

    def __init__(self) -> None:
        self.loaded: dict[str, str] = {}  # model -> expires_at
        self.up = True
        self.calls: list[object] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": m, "model": m, "expires_at": e} for m, e in self.loaded.items()]})
        body = json.loads(request.content)
        self.calls.append(body["keep_alive"])
        if body["keep_alive"] == 0:
            self.loaded.pop(body["model"], None)
        else:
            self.loaded[body["model"]] = FOREVER if body["keep_alive"] == -1 else SOON
        return httpx.Response(200, json={"model": body["model"], "done": True, "done_reason": "load"})

    def restart(self) -> None:
        self.loaded.clear()


def make(tmp_path: Path, gpu_free: list[int], running: list[bool], settings: dict | None = None):
    backend = FakeOllama()
    guard = Guard({"min_mem_available_mb": 100, "min_gpu_free_mb": 6000, "gpu_uuid": "GPU-x"},
                  mem_reader=lambda: 9000, gpu_reader=lambda _: gpu_free[0])
    res = Residency(
        {"keep_loaded_while_process": ["ffxiv_dx11.exe"], "idle_keep_alive": "5m", "poll_seconds": 1, **(settings or {})},
        "m:1", "http://ollama", httpx.AsyncClient(transport=httpx.MockTransport(backend)), guard, tmp_path / "state",
        processes=lambda wanted: list(wanted) if running[0] else [],
    )
    return res, backend


def test_state_machine(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="almanac.residency")
    asyncio.run(_state_machine(tmp_path, caplog))


async def _state_machine(tmp_path: Path, caplog) -> None:
    gpu, running = [8000], [False]
    res, backend = make(tmp_path, gpu, running)
    assert (await res.tick())["state"] == "idle" and backend.calls == []   # nothing running: no backend writes

    running[0] = True                                          # game starts: load + pin
    assert (await res.tick())["state"] == "pinned"
    assert backend.calls == [-1] and backend.loaded["m:1"] == FOREVER
    for _ in range(3):                                         # steady state: no more writes, one log line
        await res.tick()
    assert backend.calls == [-1]
    assert sum("residency pinned" in r.message for r in caplog.records) == 1
    assert read_state(tmp_path / "state")["state"] == "pinned"

    backend.loaded["m:1"] = SOON                               # a request reset keep_alive to 5m: re-pin
    await res.tick()
    assert backend.calls == [-1, -1] and backend.loaded["m:1"] == FOREVER

    backend.up = False                                         # backend restarting
    assert (await res.tick())["state"] == "backend_down"
    backend.up = True
    backend.restart()                                          # came back empty: re-pin
    assert (await res.tick())["state"] == "pinned" and backend.loaded["m:1"] == FOREVER

    running[0] = False                                         # game exits: release once
    snap = await res.tick()
    assert snap["state"] == "idle" and "released" in snap["reason"]
    assert backend.calls[-1] == "5m" and backend.loaded["m:1"] == SOON
    n = len(backend.calls)
    await res.tick()
    assert len(backend.calls) == n
    assert sum("residency idle" in r.message for r in caplog.records) == 1


def test_gpu_guard_defers(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="almanac.residency")
    asyncio.run(_gpu_guard(tmp_path, caplog))


async def _gpu_guard(tmp_path: Path, caplog) -> None:
    gpu, running = [1000], [True]
    res, backend = make(tmp_path, gpu, running)
    for _ in range(3):
        snap = await res.tick()
    assert snap["state"] == "deferred" and "GPU" in snap["reason"] and backend.calls == [] and not backend.loaded
    assert sum("residency deferred" in r.message for r in caplog.records) == 1
    gpu[0] = 7000                                              # VRAM freed: pinned on the next poll
    assert (await res.tick())["state"] == "pinned" and backend.calls == [-1]
    # Already resident: the guard is not consulted again (the model already holds its VRAM).
    gpu[0] = 0
    backend.loaded["m:1"] = SOON
    assert (await res.tick())["state"] == "pinned" and backend.calls == [-1, -1]
    # Exit while the model was unloaded meanwhile: release without loading it again.
    backend.restart()
    running[0] = False
    assert (await res.tick())["state"] == "idle" and backend.calls == [-1, -1] and not backend.loaded


def test_no_preload_and_disabled(tmp_path: Path) -> None:
    asyncio.run(_no_preload(tmp_path))


async def _no_preload(tmp_path: Path) -> None:
    res, backend = make(tmp_path, [8000], [True], {"preload": False})
    assert (await res.tick())["state"] == "idle" and backend.calls == []
    backend.loaded["m:1"] = SOON                               # a request loaded it: now pin it
    assert (await res.tick())["state"] == "pinned" and backend.calls == [-1]
    res, backend = make(tmp_path, [8000], [True], {"keep_loaded_while_process": []})
    assert (await res.tick())["state"] == "disabled" and backend.calls == []


def test_healthz_and_mcp_tool(config, tmp_path: Path) -> None:
    config.raw["residency"] = {**config.raw["residency"], "keep_loaded_while_process": ["ffxiv_dx11.exe"]}
    backend = FakeOllama()
    gw = Gateway(config, guard=Guard(config.section("guard"), mem_reader=lambda: 99999),
                 client=httpx.AsyncClient(transport=httpx.MockTransport(backend)))
    gw.residency._processes = lambda wanted: list(wanted)
    almanac = Almanac(config)
    assert "residency state unknown" in almanac.call("model_residency", {}, "test").text

    async def go() -> dict:
        await gw.residency.tick()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app()), base_url="http://gw") as client:
            return (await client.get("/healthz")).json()

    health = asyncio.run(go())
    assert health["residency"]["state"] == "pinned" and health["residency"]["model"] == "qwen3.5:9b"
    out = almanac.call("model_residency", {}, "test")
    assert not out.is_error and json.loads(out.text)["state"] == "pinned"
    assert "model_residency" in {t["name"] for t in almanac.catalogue("read")}


def test_residency_model_must_be_allowed(config) -> None:
    config.raw["residency"] = {**config.raw["residency"], "keep_loaded_while_process": ["x"], "model": "huge:70b"}
    gw = Gateway(config, client=httpx.AsyncClient(transport=httpx.MockTransport(FakeOllama())))
    assert not gw.residency.enabled
