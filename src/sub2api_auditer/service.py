from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Mapping

import httpx

from . import __version__
from .config import ConfigStore
from .normalize import parse_model_result
from .observability import TraceStore
from .protocol import (
    NormalizedAuditResult,
    ProtocolError,
    build_chat_completions_url,
    build_upstream_payload,
    extract_upstream_content,
)

LOGGER = logging.getLogger("sub2api_auditer")
MAX_UPSTREAM_RESPONSE_BYTES = 512 * 1024


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class AuditCall:
    result: NormalizedAuditResult
    raw_output: str
    latency_ms: int
    upstream_request_id: str
    trace_id: str


class UpstreamError(RuntimeError):
    def __init__(self, message: str, code: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class AuditerService:
    def __init__(
        self,
        store: ConfigStore,
        client: httpx.AsyncClient,
        traces: TraceStore | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.traces = traces or TraceStore(env_int("LOG_CAPACITY", 100))

    async def audit(
        self,
        text: str,
        *,
        trace_id: str = "",
        request_model: str = "",
        input_bytes: int = 0,
    ) -> AuditCall:
        owns_trace = not trace_id
        if owns_trace:
            trace_id = self.traces.begin(source="internal", request_model=request_model)

        config = self.store.get()
        self.traces.update_request(
            trace_id,
            request_model=request_model,
            upstream_model=config.model,
            input_chars=len(text),
            input_bytes=input_bytes or len(text.encode("utf-8")),
        )
        if not config.ready:
            error = UpstreamError(
                "审计服务尚未完成上游配置",
                "auditer_not_configured",
                status_code=503,
            )
            self.traces.mark_error(
                trace_id,
                code=error.code,
                message=str(error),
                http_status=error.status_code,
            )
            if owns_trace:
                self.traces.mark_replied(trace_id, http_status=error.status_code)
            raise error

        max_chars = env_int("MAX_INPUT_CHARS", 200_000)
        if len(text) > max_chars:
            error = UpstreamError(
                f"待审核文本超过 {max_chars} 个字符",
                "audit_input_too_large",
                status_code=413,
            )
            self.traces.mark_error(
                trace_id,
                code=error.code,
                message=str(error),
                http_status=error.status_code,
            )
            if owns_trace:
                self.traces.mark_replied(trace_id, http_status=error.status_code)
            raise error

        url = build_chat_completions_url(config.base_url)
        payload = build_upstream_payload(
            model=config.model,
            prompt=config.prompt,
            text=text,
            max_tokens=config.max_tokens,
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"Sub2apiAuditer/{__version__}",
        }
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        timeout = httpx.Timeout(
            config.timeout_seconds,
            connect=min(5.0, config.timeout_seconds),
        )
        started = time.perf_counter()
        try:
            try:
                self.traces.mark_forwarded(trace_id, upstream_model=config.model)
                async with self.client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                ) as response:
                    request_id = response.headers.get("x-request-id", "")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_UPSTREAM_RESPONSE_BYTES:
                            self.traces.mark_llm_replied(
                                trace_id,
                                upstream_http_status=response.status_code,
                                upstream_request_id=request_id,
                                response_bytes=len(body) + len(chunk),
                            )
                            raise UpstreamError(
                                "上游响应体过大",
                                "upstream_response_too_large",
                            )
                        body.extend(chunk)
                    self.traces.mark_llm_replied(
                        trace_id,
                        upstream_http_status=response.status_code,
                        upstream_request_id=request_id,
                        response_bytes=len(body),
                    )
                    if not 200 <= response.status_code < 300:
                        LOGGER.warning(
                            "upstream failed status=%s request_id=%s",
                            response.status_code,
                            request_id or "-",
                        )
                        raise UpstreamError(
                            f"上游模型网关返回 HTTP {response.status_code}",
                            "upstream_http_error",
                        )
            except httpx.TimeoutException as exc:
                raise UpstreamError(
                    "调用上游模型超时",
                    "upstream_timeout",
                    status_code=504,
                ) from exc
            except httpx.RequestError as exc:
                raise UpstreamError(
                    "无法连接上游模型网关",
                    "upstream_connection_error",
                ) from exc

            try:
                upstream = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UpstreamError(
                    "上游模型网关返回的不是有效 JSON",
                    "upstream_invalid_json",
                ) from exc
            if not isinstance(upstream, Mapping):
                raise UpstreamError(
                    "上游模型网关返回格式无效",
                    "upstream_invalid_envelope",
                )
            try:
                raw = extract_upstream_content(upstream)
                result = parse_model_result(raw)
            except ProtocolError as exc:
                raise UpstreamError(
                    str(exc),
                    "audit_model_invalid_response",
                ) from exc

            latency = max(0, int((time.perf_counter() - started) * 1000))
            self.traces.mark_result(
                trace_id,
                safety=result.safety,
                categories=result.categories,
            )
            if owns_trace:
                self.traces.mark_replied(trace_id, http_status=200)
            return AuditCall(result, raw, latency, request_id, trace_id)
        except UpstreamError as exc:
            self.traces.mark_error(
                trace_id,
                code=exc.code,
                message=str(exc),
                http_status=exc.status_code,
            )
            if owns_trace:
                self.traces.mark_replied(trace_id, http_status=exc.status_code)
            raise
