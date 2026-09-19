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
* ``/v1/messages/count_tokens`` (Claude Code calls it; Ollama has none) as a
  character-based estimate.

Request and response bodies are streamed through unchanged apart from the
``model`` field of the request.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hmac
import json
import logging
from typing import Any, AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import Config
from .guard import Guard

log = logging.getLogger("almanac.gateway")


def map_model(requested: str, settings: dict[str, Any]) -> str | None:
    """Client model name -> allowed backend model, or None if not allowed."""
    allowed = list(settings.get("allowed_models", []))
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
    def __init__(self, config: Config, guard: Guard | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.settings = config.section("gateway")
        self.backend = str(self.settings["backend"]).rstrip("/")
        self.guard = guard or Guard(config.section("guard"))
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(float(self.settings.get("request_timeout", 600)), connect=5))
        self._token = config.read_token()

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

    # -- routes ------------------------------------------------------------
    async def healthz(self, request: Request) -> Response:
        loaded = await self.loaded_models()
        return JSONResponse({"ok": True, "backend": self.backend, "loaded": loaded, "may_load": self.guard.may_load(False).reason})

    async def models(self, request: Request) -> Response:
        if not self.authorized(request):
            return _error("openai", 401, "missing or wrong token")
        names = sorted(set(self.settings.get("allowed_models", [])) | set(self.settings.get("models", {})))
        return JSONResponse(
            {"object": "list", "data": [{"id": n, "object": "model", "type": "model", "display_name": n, "owned_by": "almanac"} for n in names]}
        )

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
        body["model"] = target
        verdict = await asyncio.to_thread(self.guard.may_load, target in await self.loaded_models())
        if not verdict.ok:
            log.warning("refused %s: %s", request.url.path, verdict.reason)
            return _error(api, 503, verdict.reason)
        headers = {"content-type": "application/json"}
        for name in ("anthropic-version", "anthropic-beta", "accept"):
            if name in request.headers:
                headers[name] = request.headers[name]
        upstream = self.client.build_request("POST", f"{self.backend}{request.url.path}", json=body, headers=headers)
        try:
            response = await self.client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            return _error(api, 502, f"backend unreachable: {exc.__class__.__name__}")

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        passthrough = {k: v for k, v in response.headers.items() if k.lower() in ("content-type", "cache-control")}
        return StreamingResponse(relay(), status_code=response.status_code, headers=passthrough)

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
            task = asyncio.create_task(self.watch())
            yield
            task.cancel()
            await self.client.aclose()

        routes = [
            Route("/healthz", self.healthz),
            Route("/v1/models", self.models),
            Route("/v1/messages/count_tokens", self.count_tokens, methods=["POST"]),
            Route("/v1/messages", self.proxy, methods=["POST"]),
            Route("/v1/chat/completions", self.proxy, methods=["POST"]),
            Route("/v1/responses", self.proxy, methods=["POST"]),
        ]
        return Starlette(routes=routes, lifespan=lifespan)
