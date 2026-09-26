"""Local model gateway: Anthropic Messages + OpenAI Chat/Responses in front of Ollama.

Ollama (>= 0.14) already speaks all three wire protocols natively
(``/v1/messages``, ``/v1/chat/completions``, ``/v1/responses``), so this is a
thin, auditable proxy rather than a protocol translator. It adds only what
Ollama lacks for this use:

* bearer-token auth (``Authorization: Bearer`` or ``x-api-key``),
* model-name mapping (``claude-*`` -> a local model), and an allow-list so a
  client can never load a model that does not fit the inference GPU,
* the resource guard: refuse to *load* a model when memory is tight, and
  unload the resident model when memory runs low,
* model residency (residency.py): keep the model pinned while configured
  processes (e.g. a game) run,
* ``auto`` (autoselect.py): per request, the largest configured model that
  fits the VRAM free right now, and ``/v1/almanac/gpu`` reporting that VRAM
  so a client (the Dalamud setup) need not measure it from where it runs,
  and VRAM reservations (``/v1/almanac/gpu/reservations/<owner>``) through
  which a game on the same card makes almanac step down to a smaller model
  at once instead of on the next request,
* ``/v1/messages/count_tokens`` (Claude Code calls it; Ollama has none) as a
  character-based estimate,
* for ``/v1/responses``: Codex's tool namespaces flattened for Ollama and
  restored in the reply (see codex_compat.py).

Otherwise request and response bodies are streamed through unchanged apart
from the ``model`` field of the request.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hmac
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import codex_compat
from .autoselect import AUTO, AutoSelector, Choice, Footprints, Reservations
from .config import Config
from .gpu import MB, Gpu, inference_gpu, read_gpus
from .guard import Guard
from .residency import Residency

log = logging.getLogger("almanac.gateway")


def allowed_models(settings: dict[str, Any]) -> list[str]:
    """[gateway] allowed_models, plus auto_models and "auto" itself when auto is configured."""
    auto = [str(m) for m in settings.get("auto_models", [])]
    return list(settings.get("allowed_models", [])) + auto + ([AUTO] if auto else [])


def map_model(requested: str, settings: dict[str, Any]) -> str | None:
    """Client model name -> allowed backend model (or "auto"), or None if not allowed."""
    allowed = allowed_models(settings)
    if requested in allowed:
        return requested
    for pattern, target in dict(settings.get("models", {})).items():
        if fnmatch.fnmatchcase(requested, pattern):
            return target if target in allowed else None
    if not requested:
        return str(settings.get("default_model"))
    return None


def estimate_tokens(body: dict[str, Any]) -> int:
    """Rough token count (~4 characters per token) of an Anthropic request."""
    parts: list[str] = [json.dumps(body.get("system", "")), json.dumps(body.get("tools", []))]
    parts += [json.dumps(m.get("content", "")) for m in body.get("messages", [])]
    return max(1, sum(len(p) for p in parts) // 4)


def _error(api: str, status: int, message: str) -> JSONResponse:
    if api == "anthropic":
        kind = {401: "authentication_error", 404: "not_found_error", 400: "invalid_request_error"}.get(status, "api_error")
        return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)
    return JSONResponse({"error": {"message": message, "type": "server_error" if status >= 500 else "invalid_request_error"}}, status_code=status)


class Gateway:
    def __init__(
        self,
        config: Config,
        guard: Guard | None = None,
        client: httpx.AsyncClient | None = None,
        gpus: Callable[[], list[Gpu]] = read_gpus,
    ) -> None:
        self.settings = config.section("gateway")
        self.backend = str(self.settings["backend"]).rstrip("/")
        self.guard = guard or Guard(config.section("guard"))
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(float(self.settings.get("request_timeout", 600)), connect=5))
        self._token = config.read_token()
        self.inflight = 0  # model requests being served; /healthz reports it so clients can tell busy from free
        self._gpus = gpus
        self.gpu_uuid = str(config.section("guard").get("gpu_uuid", ""))
        self.auto = AutoSelector(self.settings, lambda: inference_gpu(self._gpus(), self.gpu_uuid), Footprints(config.state_dir),
                                 Reservations(config.state_dir))
        residency = config.section("residency")
        model = str(residency.get("model") or self.settings.get("default_model"))
        if residency.get("keep_loaded_while_process") and model not in allowed_models(self.settings):
            log.error("residency disabled: %s is not in [gateway] allowed_models", model)
            residency = {**residency, "keep_loaded_while_process": []}
        resolve = self.choose_model if model == AUTO and self.auto.enabled else None
        self.residency = Residency(residency, model, self.backend, self.client, self.guard, config.state_dir, resolve=resolve)

    # -- helpers -----------------------------------------------------------
    def authorized(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        return bool(supplied) and hmac.compare_digest(supplied.encode(), self._token.encode())

    async def loaded_models(self) -> list[str]:
        try:
            response = await self.client.get(f"{self.backend}/api/ps", timeout=5)
            return [m["name"] for m in response.json().get("models", [])]
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    async def unload(self, model: str) -> None:
        with contextlib.suppress(httpx.HTTPError):
            await self.client.post(f"{self.backend}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)

    async def backend_state(self) -> tuple[dict[str, int], dict[str, int]]:
        """(installed: model -> file bytes from /api/tags, loaded: model -> VRAM bytes from /api/ps); empty on errors."""
        async def read(path: str, size_key: str) -> dict[str, int]:
            try:
                response = await self.client.get(f"{self.backend}{path}", timeout=5)
                return {str(m["name"]): int(m.get(size_key) or 0) for m in response.json().get("models", [])}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                return {}
        installed, loaded = await asyncio.gather(read("/api/tags", "size"), read("/api/ps", "size_vram"))
        return installed, loaded

    async def choose(self) -> tuple[Choice, dict[str, int]]:
        """The auto model for right now, and what is loaded (for the caller to make room)."""
        installed, loaded = await self.backend_state()
        return await asyncio.to_thread(self.auto.choose, installed, loaded), loaded

    async def choose_model(self) -> str:
        return (await self.choose())[0].model

    async def step_down(self) -> Choice | None:
        """Unload our resident auto models that are not the one that fits now. The fitting one
        loads on the next request, as after any unload."""
        if not self.auto.enabled:
            return None
        choice, loaded = await self.choose()
        for model in loaded:
            if model in self.auto.models and model != choice.model:
                log.info("unloading %s to make room: %s", model, choice.reason)
                await self.unload(model)
        return choice

    # -- routes ------------------------------------------------------------
    async def healthz(self, request: Request) -> Response:
        loaded = await self.loaded_models()
        return JSONResponse({"ok": True, "backend": self.backend, "loaded": loaded, "may_load": self.guard.may_load(False).reason,
                             "inflight": self.inflight,
                             "residency": self.residency.snapshot()})

    async def models(self, request: Request) -> Response:
        if not self.authorized(request):
            return _error("openai", 401, "missing or wrong token")
        names = sorted(set(allowed_models(self.settings)) | {m for m in self.settings.get("models", {}) if not any(c in m for c in "*?[")})
        data = [{"id": n, "object": "model", "type": "model", "display_name": n, "owned_by": "almanac"} for n in names]
        # "data": OpenAI/Anthropic list shape. "models": Codex's catalogue field;
        # left empty so Codex falls back to its own/model_catalog_json metadata.
        return JSONResponse({"object": "list", "data": data, "has_more": False, "models": []})

    async def gpu(self, request: Request) -> Response:
        """The inference GPU as this machine sees it, and what a model may use of it (see gpu.py)."""
        if not self.authorized(request):
            return _error("openai", 401, "missing or wrong token")
        gpus = await asyncio.to_thread(self._gpus)
        card = inference_gpu(gpus, self.gpu_uuid)
        installed, loaded = await self.backend_state()
        body: dict[str, Any] = {
            "gpus": [g.to_json() for g in gpus],
            "inference": card.to_json() if card else None,
            "loaded": [{"model": name, "vram_mb": size // MB} for name, size in loaded.items()],
            "installed": sorted(installed),
        }
        body["reservations"] = self.auto.reservations.current()
        if card is not None:
            # Switching models gives back what the resident ones hold; the headroom stays free for everything else,
            # and what other workloads reserved is off limits.
            reclaim = sum(loaded.values()) // MB
            headroom = self.auto.headroom_mb(card.total_mb)
            budget = self.auto.budget(card, loaded) if self.auto.enabled else card.free_mb + reclaim - headroom
            body.update({"reclaimable_mb": reclaim, "headroom_mb": headroom, "budget_mb": max(0, budget)})
        if self.auto.enabled:
            choice = await asyncio.to_thread(self.auto.choose, installed, loaded)
            body["auto"] = {"models": self.auto.models, "fallback": self.auto.fallback, "choice": choice.model, "reason": choice.reason}
        return JSONResponse(body)

    async def reservation(self, request: Request) -> Response:
        """PUT {"mb": N, "ttl_s": optional seconds} holds N MB of the card for ``owner``; DELETE releases it."""
        if not self.authorized(request):
            return _error("openai", 401, "missing or wrong token")
        owner = request.path_params["owner"]
        if request.method == "DELETE":
            released = self.auto.reservations.release(owner)
            return JSONResponse({"owner": owner, "released": released, "reservations": self.auto.reservations.current()})
        try:
            body = await request.json()
            mb = int(body["mb"])
            ttl = body.get("ttl_s")
            ttl = None if ttl is None else float(ttl)
        except (ValueError, KeyError, TypeError):
            return _error("openai", 400, 'body must be {"mb": <int>, "ttl_s": <seconds, optional>}')
        if mb < 0 or (ttl is not None and ttl <= 0):
            return _error("openai", 400, "mb must be >= 0 and ttl_s > 0")
        self.auto.reservations.hold(owner, mb, ttl)
        choice = await self.step_down()
        return JSONResponse({"owner": owner, "mb": mb, "ttl_s": ttl, "reservations": self.auto.reservations.current(),
                             "auto": None if choice is None else {"choice": choice.model, "reason": choice.reason}})

    async def count_tokens(self, request: Request) -> Response:
        if not self.authorized(request):
            return _error("anthropic", 401, "missing or wrong token")
        return JSONResponse({"input_tokens": estimate_tokens(await request.json())})

    async def proxy(self, request: Request) -> Response:
        api = "anthropic" if request.url.path.startswith("/v1/messages") else "openai"
        if not self.authorized(request):
            return _error(api, 401, "missing or wrong token")
        try:
            body = await request.json()
        except ValueError:
            return _error(api, 400, "request body is not JSON")
        requested = str(body.get("model", ""))
        target = map_model(requested, self.settings)
        if target is None:
            return _error(api, 404, f"model {requested!r} is not mapped to an allowed local model")
        picked_by_auto = target == AUTO and self.auto.enabled
        if picked_by_auto:
            choice, loaded = await self.choose()
            target = choice.model
            log.info("auto -> %s: %s", target, choice.reason)
            # Make room: our other auto models go first, so switching never needs both in VRAM.
            for other in loaded:
                if other in self.auto.models and other != target:
                    await self.unload(other)
            resident = target in loaded
        else:
            resident = target in await self.loaded_models()
        body["model"] = target
        namespaces = codex_compat.flatten_request(body) if request.url.path == "/v1/responses" else {}
        verdict = await asyncio.to_thread(self.guard.may_load, resident, picked_by_auto)
        if not verdict.ok:
            log.warning("refused %s: %s", request.url.path, verdict.reason)
            return _error(api, 503, verdict.reason)
        headers = {"content-type": "application/json"}
        for name in ("anthropic-version", "anthropic-beta", "accept"):
            if name in request.headers:
                headers[name] = request.headers[name]
        upstream = self.client.build_request("POST", f"{self.backend}{request.url.path}", json=body, headers=headers)
        self.inflight += 1
        try:
            response = await self.client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            self.inflight -= 1
            return _error(api, 502, f"backend unreachable: {exc.__class__.__name__}")
        except BaseException:
            self.inflight -= 1
            raise

        open_ = [True]

        async def done() -> None:  # once, from the stream's end or (if it never started) after the response
            if open_:
                open_.clear()
                self.inflight -= 1
                await response.aclose()

        async def relay() -> AsyncIterator[bytes]:
            try:
                if not namespaces:
                    async for chunk in response.aiter_bytes():
                        yield chunk
                elif response.headers.get("content-type", "").startswith("text/event-stream"):
                    async for chunk in codex_compat.restore_stream(response.aiter_bytes(), namespaces):
                        yield chunk
                else:
                    data = json.loads(await response.aread())
                    yield json.dumps(codex_compat.restore(data, namespaces)).encode()
            finally:
                await done()

        passthrough = {k: v for k, v in response.headers.items() if k.lower() in ("content-type", "cache-control")}
        return StreamingResponse(relay(), status_code=response.status_code, headers=passthrough, background=BackgroundTask(done))

    # -- memory watch --------------------------------------------------------
    async def watch(self) -> None:
        while True:
            await asyncio.sleep(self.guard.poll)
            try:
                if self.guard.should_unload():
                    for model in await self.loaded_models():
                        log.warning("memory is low; unloading %s", model)
                        await self.unload(model)
            except Exception:  # keep the watcher alive whatever happens
                log.exception("memory watch failed")

    def app(self) -> Starlette:
        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            tasks = [asyncio.create_task(self.watch()), asyncio.create_task(self.residency.run())]
            yield
            for task in tasks:
                task.cancel()
            await self.client.aclose()

        routes = [
            Route("/healthz", self.healthz),
            Route("/v1/models", self.models),
            Route("/v1/almanac/gpu", self.gpu),
            Route("/v1/almanac/gpu/reservations/{owner}", self.reservation, methods=["PUT", "DELETE"]),
            Route("/v1/messages/count_tokens", self.count_tokens, methods=["POST"]),
            Route("/v1/messages", self.proxy, methods=["POST"]),
            Route("/v1/chat/completions", self.proxy, methods=["POST"]),
            Route("/v1/responses", self.proxy, methods=["POST"]),
        ]
        return Starlette(routes=routes, lifespan=lifespan)
