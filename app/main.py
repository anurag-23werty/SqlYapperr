"""FastAPI application factory, middleware and safe error handling."""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.agent.llm_interface import SQLGenerator
from app.api.routes import router
from app.config import Settings, get_settings
from app.container import VERSION, build_container

log = logging.getLogger("app")


class RequestContextMiddleware:
    """Assigns a server-generated request id and enforces a hard body-size cap.

    The body is buffered up to ``max_body_bytes + 1`` bytes, so an oversized or
    lying ``Content-Length`` cannot make the server read an unbounded body.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                ]
                message["headers"] = headers
            await send(message)

        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_body_bytes
            except ValueError:
                too_big = True
            if too_big:
                await _send_error(send_with_headers, 413, request_id, "PAYLOAD_TOO_LARGE", "request body too large")
                return

        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            more = message.get("more_body", False)
            if len(body) > self.max_body_bytes:
                await _send_error(send_with_headers, 413, request_id, "PAYLOAD_TOO_LARGE", "request body too large")
                return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send_with_headers)


async def _send_error(send: Send, status: int, request_id: str, code: str, message: str) -> None:
    payload = json.dumps({"request_id": request_id, "error": {"code": code, "message": message}}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]})
    await send({"type": "http.response.body", "body": payload})


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def create_app(settings: Settings | None = None, generator: SQLGenerator | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    app = FastAPI(title="SQLYapper", version=VERSION)
    # Built once at startup; every component in the container is immutable/stateless.
    app.state.container = build_container(settings, generator)
    app.include_router(router)
    app.add_middleware(RequestContextMiddleware, max_body_bytes=settings.max_body_bytes)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Report field locations and messages only; never echo the submitted input.
        details = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()) if p != 'body')}: {err.get('msg', 'invalid')}"
            for err in exc.errors()[:5]
        )
        return JSONResponse(status_code=422, content={"request_id": _rid(request),
                                                      "error": {"code": "INVALID_REQUEST", "message": details}})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        return JSONResponse(status_code=exc.status_code, content={"request_id": _rid(request),
                                                                  "error": {"code": f"HTTP_{exc.status_code}", "message": message}})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error request_id=%s", _rid(request))
        return JSONResponse(status_code=500, content={"request_id": _rid(request),
                                                      "error": {"code": "INTERNAL_ERROR", "message": "internal server error"}})

    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Lazily build ``app`` so ``uvicorn app.main:app`` works without import side effects."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
