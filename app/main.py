"""FastAPI application factory, middleware and safe error handling."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.agent.llm import LLMClient
from app.api.routes import router
from app.config import Settings, get_settings
from app.observability import metrics
from app.observability.context import request_scope
from app.observability.logs import setup_logging
from app.observability.tracing import setup_tracing
from app.services import VERSION, build_services

log = logging.getLogger("app")
access_log = logging.getLogger("app.access")
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


class RequestContextMiddleware:
    """Server-side request id, security headers, and a hard body-size cap.

    The body is buffered up to the limit + 1 byte, so an oversized or lying
    ``Content-Length`` cannot make the server read an unbounded body. Dataset
    uploads get their own (larger) limit; every other route gets the small one.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int, max_upload_bytes: int, csp_enabled: bool = True) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_upload_bytes = max_upload_bytes
        self.csp_enabled = csp_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status_holder = {"status": 500}
        with request_scope(request_id):
            try:
                await self._handle(scope, receive, send, request_id, status_holder)
            finally:
                self._observe(scope, status_holder["status"], time.perf_counter() - started)

    @staticmethod
    def _observe(scope: Scope, status: int, elapsed: float) -> None:
        route = scope.get("route")
        path: str = scope.get("path", "")
        template = getattr(route, "path", None) or ("/static" if path.startswith("/static/") else "unmatched")
        method = scope.get("method", "GET")
        metrics.HTTP_REQUESTS.inc(method=method, route=template, status=str(status))
        metrics.HTTP_LATENCY.observe(elapsed, method=method, route=template)
        if template not in {"/health", "/ready", "/metrics", "/static"}:
            access_log.info(
                "request completed",
                extra={"method": method, "route": template, "status": status, "duration_ms": round(elapsed * 1000, 1)},
            )

    async def _handle(
        self, scope: Scope, receive: Receive, send: Send, request_id: str, status_holder: dict[str, int]
    ) -> None:
        path: str = scope.get("path", "")
        is_upload = scope.get("method") == "POST" and path.rstrip("/") == "/api/datasets"
        limit = self.max_upload_bytes if is_upload else self.max_body_bytes
        docs = path.startswith(("/docs", "/openapi.json", "/redoc"))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message.get("status", 500))
                headers = list(message.get("headers", []))
                headers += [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                ]
                if path.startswith("/api/"):
                    headers.append((b"cache-control", b"no-store"))
                if self.csp_enabled and not docs:
                    headers.append((b"content-security-policy", _CSP.encode()))
                message["headers"] = headers
            await send(message)

        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
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
            if len(body) > limit:
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


def create_app(
    settings: Settings | None = None, llm: LLMClient | None = None, use_langgraph: bool | None = None
) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_format)
    setup_tracing(settings.otel_exporter_endpoint, settings.otel_service_name, settings.env)
    production = settings.env == "production"

    app = FastAPI(
        title="SQLYapper", version=VERSION,
        docs_url=None if production else "/docs", redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )
    app.state.services = build_services(settings, llm=llm, use_langgraph=use_langgraph)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_middleware(
        RequestContextMiddleware,
        max_body_bytes=settings.max_body_bytes,
        max_upload_bytes=settings.max_upload_mb * 1024 * 1024 + 64 * 1024,  # + multipart overhead
    )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only; never echo submitted input (it may contain passwords).
        details = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()) if p != 'body')}: {err.get('msg', 'invalid')}"
            for err in exc.errors()[:5]
        )
        return JSONResponse(status_code=422, content={"request_id": _rid(request),
                                                      "error": {"code": "INVALID_REQUEST", "message": details}})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            error = {"code": str(exc.detail.get("code", f"HTTP_{exc.status_code}")),
                     "message": str(exc.detail.get("message", "request failed"))}
        else:
            error = {"code": f"HTTP_{exc.status_code}",
                     "message": exc.detail if isinstance(exc.detail, str) else "request failed"}
        return JSONResponse(status_code=exc.status_code, content={"request_id": _rid(request), "error": error})

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
