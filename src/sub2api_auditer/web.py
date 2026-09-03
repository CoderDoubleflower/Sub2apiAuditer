from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from importlib.resources import files
from typing import Any, Mapping

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import __version__
from .config import ConfigConflict, ConfigError, ConfigStore
from .protocol import ProtocolError, extract_audit_text, make_openai_error, make_openai_response
from .service import AuditerService, UpstreamError, env_int

LOGGER = logging.getLogger("sub2api_auditer")


def _bearer(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-admin-token", "").strip()


def _authorized(request: Request, expected: str) -> bool:
    if not expected:
        return True
    supplied = _bearer(request)
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(make_openai_error(message, code), status_code=status)


async def _json(request: Request) -> Mapping[str, Any]:
    limit = env_int("MAX_REQUEST_BODY_BYTES", 2 * 1024 * 1024)
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > limit:
                raise ProtocolError("请求体过大")
        except ValueError:
            pass
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise ProtocolError("请求体过大")
        body.extend(chunk)
    try:
        value = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise ProtocolError("请求体不是有效 JSON") from exc
    if not isinstance(value, Mapping):
        raise ProtocolError("请求体根节点必须是 JSON 对象")
    return value


@lru_cache(maxsize=3)
def _static(name: str) -> str:
    return files("sub2api_auditer.static").joinpath(name).read_text(encoding="utf-8")


async def home(request: Request) -> Response:
    del request
    return HTMLResponse(_static("index.html"))


async def asset(request: Request) -> Response:
    name = request.path_params["name"]
    if name == "app.css":
        return PlainTextResponse(_static(name), media_type="text/css")
    if name == "app.js":
        return PlainTextResponse(_static(name), media_type="application/javascript")
    return Response(status_code=404)


async def healthz(request: Request) -> Response:
    del request
    return JSONResponse({"status": "ok", "version": __version__})


async def readyz(request: Request) -> Response:
    store: ConfigStore = request.app.state.store
    config = store.get()
    status = 200 if config.ready and not store.load_error else 503
    return JSONResponse({
        "status": "ready" if status == 200 else "not_ready",
        "configured": config.ready,
        "config_version": config.version,
        "config_error": store.load_error,
    }, status_code=status)


async def get_config(request: Request) -> Response:
    if not _authorized(request, request.app.state.admin_token):
        return _error("管理员令牌无效", "admin_unauthorized", 401)
    store: ConfigStore = request.app.state.store
    return JSONResponse({
        "config": store.get().public_dict(),
        "config_error": store.load_error,
        "admin_auth_enabled": bool(request.app.state.admin_token),
        "proxy_auth_enabled": bool(request.app.state.auditer_token),
    })


async def put_config(request: Request) -> Response:
    if not _authorized(request, request.app.state.admin_token):
        return _error("管理员令牌无效", "admin_unauthorized", 401)
    try:
        config = await request.app.state.store.update(await _json(request))
    except ConfigConflict as exc:
        return _error(str(exc), "config_conflict", 409)
    except (ConfigError, ProtocolError, TypeError, ValueError) as exc:
        return _error(str(exc), "invalid_config", 400)
    except OSError:
        LOGGER.exception("failed to persist config")
        return _error("配置写入失败", "config_write_failed", 500)
    return JSONResponse({"ok": True, "config": config.public_dict()})


async def status(request: Request) -> Response:
    if not _authorized(request, request.app.state.admin_token):
        return _error("管理员令牌无效", "admin_unauthorized", 401)
    store: ConfigStore = request.app.state.store
    service: AuditerService = request.app.state.service
    return JSONResponse({
        "version": __version__,
        "ready": store.get().ready and not store.load_error,
        "config_version": store.get().version,
        "config_error": store.load_error,
        "stats": service.stats.public_dict(),
    })


async def test_audit(request: Request) -> Response:
    if not _authorized(request, request.app.state.admin_token):
        return _error("管理员令牌无效", "admin_unauthorized", 401)
    try:
        text = str((await _json(request)).get("text", "")).strip()
        if not text:
            raise ProtocolError("测试文本不能为空")
        call = await request.app.state.service.audit(text)
    except ProtocolError as exc:
        return _error(str(exc), "invalid_test_input", 400)
    except UpstreamError as exc:
        return _error(str(exc), exc.code, exc.status_code)
    return JSONResponse({
        "ok": True,
        "normalized": call.result.as_dict(),
        "raw_model_output": call.raw_output[:8000],
        "latency_ms": call.latency_ms,
        "upstream_request_id": call.upstream_request_id,
    })


async def models(request: Request) -> Response:
    if not _authorized(request, request.app.state.auditer_token):
        return _error("审计服务访问令牌无效", "unauthorized", 401)
    configured = request.app.state.store.get().model
    ids = list(dict.fromkeys(value for value in (configured, "sub2api-auditer") if value))
    return JSONResponse({
        "object": "list",
        "data": [{"id": value, "object": "model", "created": 0, "owned_by": "sub2api-auditer"} for value in ids],
    })


async def completions(request: Request) -> Response:
    if not _authorized(request, request.app.state.auditer_token):
        return _error("审计服务访问令牌无效", "unauthorized", 401)
    try:
        payload = await _json(request)
        call = await request.app.state.service.audit(extract_audit_text(payload))
    except ProtocolError as exc:
        return _error(str(exc), "invalid_audit_request", 413 if "过大" in str(exc) else 400)
    except ConfigError as exc:
        return _error(str(exc), "invalid_upstream_config", 503)
    except UpstreamError as exc:
        return _error(str(exc), exc.code, exc.status_code)
    except Exception:
        LOGGER.exception("unexpected audit failure")
        return _error("审计服务发生内部错误", "internal_error", 500)

    response = JSONResponse(make_openai_response(
        result=call.result,
        request_model=str(payload.get("model", "") or "sub2api-auditer"),
    ))
    response.headers["X-Auditer-Latency-Ms"] = str(call.latency_ms)
    response.headers["X-Auditer-Version"] = __version__
    if call.upstream_request_id:
        response.headers["X-Upstream-Request-Id"] = call.upstream_request_id[:256]
    return response


def create_app(
    *,
    config_path: str | None = None,
    client: httpx.AsyncClient | None = None,
    admin_token: str | None = None,
    auditer_token: str | None = None,
) -> Starlette:
    store = ConfigStore(config_path or os.getenv("CONFIG_PATH", "./data/config.json"))
    owns_client = client is None

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await store.load()
        app.state.store = store
        app.state.admin_token = os.getenv("ADMIN_TOKEN", "").strip() if admin_token is None else admin_token.strip()
        app.state.auditer_token = os.getenv("AUDITER_TOKEN", "").strip() if auditer_token is None else auditer_token.strip()
        app.state.client = client or httpx.AsyncClient(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=30.0),
            follow_redirects=False,
            trust_env=False,
        )
        app.state.service = AuditerService(store, app.state.client)
        LOGGER.info(
            "started version=%s configured=%s admin_auth=%s proxy_auth=%s",
            __version__, store.get().ready, bool(app.state.admin_token), bool(app.state.auditer_token),
        )
        try:
            yield
        finally:
            if owns_client:
                await app.state.client.aclose()

    routes = [
        Route("/", home, methods=["GET"]),
        Route("/assets/{name}", asset, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
        Route("/api/config", get_config, methods=["GET"]),
        Route("/api/config", put_config, methods=["PUT"]),
        Route("/api/status", status, methods=["GET"]),
        Route("/api/test", test_audit, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/models", models, methods=["GET"]),
        Route("/v1/chat/completions", completions, methods=["POST"]),
        Route("/chat/completions", completions, methods=["POST"]),
    ]
    return Starlette(debug=False, routes=routes, lifespan=lifespan)
