from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from importlib.resources import files
from typing import Any, Mapping

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .config import ConfigConflict, ConfigError, ConfigStore
from .observability import PendingTrace, TracePersistenceError, TraceStore
from .protocol import ProtocolError, extract_audit_text, make_openai_error, make_openai_response
from .service import AuditerService, UpstreamError, env_int

LOGGER = logging.getLogger("sub2api_auditer")
_TRACE_KEY = "sub2api_auditer.trace_id"


def _bearer(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-admin-token", "").strip()


def _authorized(request: Request, expected: str) -> bool:
    if not expected:
        return True
    supplied = _bearer(request)
    # compare_digest(str, str) raises for non-ASCII. Invalid input is a 401,
    # never an exception outside the audit lifecycle's error handling.
    if not supplied or not supplied.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in supplied):
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(make_openai_error(message, code), status_code=status)


class _TraceResponse(Response):
    """Timestamp the final ASGI send before any worker-thread scheduling."""

    def __init__(self, response: Response, traces: TraceStore, trace_id: str) -> None:
        self.response = response
        self.traces = traces
        self.trace_id = trace_id
        self.status_code = response.status_code
        self.raw_headers = response.raw_headers
        self.body = response.body
        self.background = response.background

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        pending: PendingTrace | None = None
        failure_status = 500

        async def traced_send(message: Message) -> None:
            nonlocal pending
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                pending = self.traces.finish(self.trace_id, http_status=self.status_code)

        try:
            await self.response(scope, receive, traced_send)
        except asyncio.CancelledError:
            failure_status = 499
            self.traces.mark_error(self.trace_id, code="audit_cancelled", message="响应任务被取消", http_status=499)
            raise
        except Exception:
            self.traces.mark_error(self.trace_id, code="response_send_failed", message="响应发送失败", http_status=500)
            raise
        finally:
            if pending is None:
                pending = self.traces.finish(self.trace_id, http_status=failure_status, sent=False)
            await self.traces.persist_async(pending)


class _TraceLifecycleMiddleware:
    """Finalize requests cancelled or disconnected before a Response exists."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        code, message, status_code = "internal_error", "请求未正常完成", 500
        try:
            await self.app(scope, receive, send)
        except asyncio.CancelledError:
            code, message, status_code = "audit_cancelled", "审核任务被取消", 499
            raise
        except ClientDisconnect:
            code, message, status_code = "client_disconnected", "请求客户端已断开", 499
            # The peer is gone; do not manufacture a sent-response timestamp.
        finally:
            trace_id = scope.get(_TRACE_KEY)
            if trace_id:
                traces = scope["app"].state.service.traces
                traces.mark_error(trace_id, code=code, message=message, http_status=status_code)
                pending = traces.finish(trace_id, http_status=status_code, sent=False)
                await traces.persist_async(pending)


def _traced_response(response: Response, service: AuditerService, trace_id: str) -> Response:
    response.headers["X-Auditer-Trace-Id"] = trace_id
    return _TraceResponse(response, service.traces, trace_id)


def _traced_error(service: AuditerService, trace_id: str, *, message: str, code: str, status: int) -> Response:
    service.traces.mark_error(trace_id, code=code, message=message, http_status=status)
    return _traced_response(_error(message, code, status), service, trace_id)


async def _read_json(request: Request) -> tuple[Mapping[str, Any], int]:
    limit = env_int("MAX_REQUEST_BODY_BYTES", 2 * 1024 * 1024)
    length = request.headers.get("content-length")
    if length:
        try:
            declared = int(length)
        except ValueError:
            declared = 0
        # Keep ProtocolError outside the ValueError handler (it subclasses it).
        if declared > limit:
            raise ProtocolError("请求体过大")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise ProtocolError("请求体过大")
        body.extend(chunk)
    try:
        value = json.loads(body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("请求体不是有效 JSON") from exc
    if not isinstance(value, Mapping):
        raise ProtocolError("请求体根节点必须是 JSON 对象")
    return value, len(body)


async def _json(request: Request) -> Mapping[str, Any]:
    value, _ = await _read_json(request)
    return value


@lru_cache(maxsize=3)
def _static(name: str) -> str:
    return files("sub2api_auditer.static").joinpath(name).read_text(encoding="utf-8")


async def home(request: Request) -> Response:
    del request
    response = HTMLResponse(_static("index.html"))
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors *; base-uri 'self'; form-action 'self'"
    )
    return response


async def asset(request: Request) -> Response:
    name = request.path_params["name"]
    if name not in {"app.css", "app.js"}:
        return Response(status_code=404)
    response = PlainTextResponse(_static(name), media_type="text/css" if name == "app.css" else "application/javascript")
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


async def healthz(request: Request) -> Response:
    del request
    return JSONResponse({"status": "ok", "version": __version__})


async def readyz(request: Request) -> Response:
    store: ConfigStore = request.app.state.store
    return JSONResponse({"status": "ready" if store.ready else "not_ready", "configured": store.get().ready,
                         "config_version": store.get().version, "config_error": store.load_error},
                        status_code=200 if store.ready else 503)


async def get_config(request: Request) -> Response:
    if not _authorized(request, request.app.state.admin_token):
        return _error("管理员令牌无效", "admin_unauthorized", 401)
    store: ConfigStore = request.app.state.store
    return JSONResponse({"config": store.get().public_dict(), "config_error": store.load_error,
                         "admin_auth_enabled": bool(request.app.state.admin_token),
                         "proxy_auth_enabled": bool(request.app.state.auditer_token)})


async def put_config(request: Request) -> Response:
    if not _authorized(request, request.app.state.admin_token):
        return _error("管理员令牌无效", "admin_unauthorized", 401)
    try:
        config = await request.app.state.store.update(await _json(request))
    except ConfigConflict as exc:
        return _error(str(exc), "config_conflict", 409)
    except (ConfigError, ProtocolError, TypeError, ValueError, OverflowError) as exc:
        return _error(str(exc), "invalid_config", 400)
    except OSError:
        LOGGER.exception("failed to persist config")
        return _error("配置写入失败", "config_write_failed", 500)
    return JSONResponse({"ok": True, "config": config.public_dict()})


async def status(request: Request) -> Response:
    if not _authorized(request, request.app.state.admin_token):
        return _error("管理员令牌无效", "admin_unauthorized", 401)
    store: ConfigStore = request.app.state.store
    return JSONResponse({"version": __version__, "ready": store.ready, "config_version": store.get().version,
                         "config_error": store.load_error, "stats": request.app.state.service.traces.runtime_stats()})


async def processing_logs(request: Request) -> Response:
    if not _authorized(request, request.app.state.admin_token):
        return _error("管理员令牌无效", "admin_unauthorized", 401)
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    traces = request.app.state.service.traces
    return JSONResponse({"items": traces.list(limit=limit), "capacity": traces.capacity,
                         "persistence": traces.persistence})


async def processing_statistics(request: Request) -> Response:
    if not _authorized(request, request.app.state.admin_token):
        return _error("管理员令牌无效", "admin_unauthorized", 401)
    return JSONResponse(request.app.state.service.traces.statistics())


async def clear_processing_logs(request: Request) -> Response:
    if not _authorized(request, request.app.state.admin_token):
        return _error("管理员令牌无效", "admin_unauthorized", 401)
    try:
        cleared = await asyncio.to_thread(request.app.state.service.traces.clear)
    except TracePersistenceError as exc:
        return _error(str(exc), "trace_clear_failed", 500)
    return JSONResponse({"ok": True, "cleared": cleared})


async def _audit_request(request: Request, *, manual: bool) -> Response:
    service: AuditerService = request.app.state.service
    trace_id = service.traces.begin(source="manual_test" if manual else "sub2api",
                                    client_request_id=request.headers.get("x-request-id", "")
                                    or request.headers.get("x-correlation-id", ""))
    request.scope[_TRACE_KEY] = trace_id
    token = request.app.state.admin_token if manual else request.app.state.auditer_token
    if not _authorized(request, token):
        return _traced_error(service, trace_id, message="管理员令牌无效" if manual else "审计服务访问令牌无效",
                             code="admin_unauthorized" if manual else "unauthorized", status=401)
    try:
        payload, body_bytes = await _read_json(request)
        request_model = "manual-test" if manual else str(payload.get("model", "") or "sub2api-auditer")
        text = str(payload.get("text", "")).strip() if manual else extract_audit_text(payload)
        if not text:
            raise ProtocolError("测试文本不能为空")
        call = await service.audit(text, trace_id=trace_id, request_model=request_model, input_bytes=body_bytes)
        if manual:
            response = JSONResponse({"ok": True, "trace_id": call.trace_id, "normalized": call.result.as_dict(),
                                     "raw_model_output": call.raw_output[:8000], "latency_ms": call.latency_ms,
                                     "upstream_request_id": call.upstream_request_id})
        else:
            response = JSONResponse(make_openai_response(result=call.result, request_model=request_model))
            response.headers["X-Auditer-Latency-Ms"] = str(call.latency_ms)
            response.headers["X-Auditer-Version"] = __version__
            if call.upstream_request_id:
                response.headers["X-Upstream-Request-Id"] = call.upstream_request_id[:256]
        return _traced_response(response, service, trace_id)
    except ClientDisconnect:
        raise
    except ProtocolError as exc:
        return _traced_error(service, trace_id, message=str(exc),
                             code="invalid_test_input" if manual else "invalid_audit_request",
                             status=413 if "过大" in str(exc) else 400)
    except ConfigError as exc:
        return _traced_error(service, trace_id, message=str(exc), code="invalid_upstream_config", status=503)
    except UpstreamError as exc:
        return _traced_error(service, trace_id, message=str(exc), code=exc.code, status=exc.status_code)
    except Exception:
        LOGGER.exception("unexpected audit failure")
        return _traced_error(service, trace_id, message="审计服务发生内部错误", code="internal_error", status=500)


async def test_audit(request: Request) -> Response:
    return await _audit_request(request, manual=True)


async def completions(request: Request) -> Response:
    return await _audit_request(request, manual=False)


async def models(request: Request) -> Response:
    if not _authorized(request, request.app.state.auditer_token):
        return _error("审计服务访问令牌无效", "unauthorized", 401)
    configured = request.app.state.store.get().model
    ids = list(dict.fromkeys(value for value in (configured, "sub2api-auditer") if value))
    return JSONResponse({"object": "list", "data": [{"id": value, "object": "model", "created": 0,
                                                    "owned_by": "sub2api-auditer"} for value in ids]})


def create_app(*, config_path: str | None = None, client: httpx.AsyncClient | None = None,
               admin_token: str | None = None, auditer_token: str | None = None,
               base_path: str | None = None) -> Starlette:
    store = ConfigStore(config_path or os.getenv("CONFIG_PATH", "./data/config.json"))
    owns_client = client is None
    prefix = (os.getenv("BASE_PATH", "") if base_path is None else base_path).strip().rstrip("/")
    if prefix and (not re.fullmatch(r"/[A-Za-z0-9_/-]+", prefix) or "//" in prefix):
        raise ConfigError("BASE_PATH 必须是 /auditer 这样的路径前缀")

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await store.load()
        # Opening/restoring SQLite is not part of the request event loop.
        traces = await asyncio.to_thread(TraceStore, env_int("LOG_CAPACITY", 100))
        app.state.store = store
        app.state.admin_token = (os.getenv("ADMIN_TOKEN", "") if admin_token is None else admin_token).strip()
        app.state.auditer_token = (os.getenv("AUDITER_TOKEN", "") if auditer_token is None else auditer_token).strip()
        for token in (app.state.admin_token, app.state.auditer_token):
            if token and (not token.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in token)):
                raise ConfigError("ADMIN_TOKEN 和 AUDITER_TOKEN 必须使用不含控制字符的 ASCII 字符")
        app.state.client = client if client is not None else httpx.AsyncClient(
            limits=httpx.Limits(max_connections=env_int("HTTP_MAX_CONNECTIONS", 200),
                               max_keepalive_connections=env_int("HTTP_MAX_KEEPALIVE", 50), keepalive_expiry=30.0),
            follow_redirects=False, trust_env=False)
        app.state.service = AuditerService(store, app.state.client, traces)
        LOGGER.info("started version=%s configured=%s admin_auth=%s proxy_auth=%s persistence=%s",
                    __version__, store.ready, bool(app.state.admin_token), bool(app.state.auditer_token), traces.persistence)
        try:
            yield
        finally:
            if owns_client:
                await app.state.client.aclose()

    routes = [
        Route("/", home, methods=["GET"]), Route("/assets/{name}", asset, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]), Route("/readyz", readyz, methods=["GET"]),
        Route("/api/config", get_config, methods=["GET"]), Route("/api/config", put_config, methods=["PUT"]),
        Route("/api/status", status, methods=["GET"]), Route("/api/logs", processing_logs, methods=["GET"]),
        Route("/api/logs", clear_processing_logs, methods=["DELETE"]),
        Route("/api/statistics", processing_statistics, methods=["GET"]), Route("/api/test", test_audit, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]), Route("/models", models, methods=["GET"]),
        Route("/v1/chat/completions", completions, methods=["POST"]), Route("/chat/completions", completions, methods=["POST"]),
    ]
    if prefix:
        # Docker healthcheck stays reachable at /healthz even when mounted.
        routes = [Route("/healthz", healthz, methods=["GET"]), Mount(prefix, routes=routes)]
    return Starlette(debug=False, routes=routes, lifespan=lifespan,
                     middleware=[Middleware(_TraceLifecycleMiddleware)])
