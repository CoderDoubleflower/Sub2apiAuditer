from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Mapping

import httpx

from . import __version__
from .config import AuditConfig, ConfigStore
from .normalize import parse_model_result
from .observability import TraceStore
from .protocol import (NormalizedAuditResult, ProtocolError, build_chat_completions_url,
                       build_upstream_payload, extract_upstream_content)

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
    def __init__(self, store: ConfigStore, client: httpx.AsyncClient,
                 traces: TraceStore | None = None) -> None:
        self.store = store
        self.client = client
        self.traces = traces if traces is not None else TraceStore(env_int("LOG_CAPACITY", 100))

    async def _upstream(self, config: AuditConfig, text: str, trace_id: str) -> tuple[bytearray, str]:
        url = build_chat_completions_url(config.base_url)
        payload = build_upstream_payload(model=config.model, prompt=config.prompt, text=text,
                                         max_tokens=config.max_tokens)
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": f"Sub2apiAuditer/{__version__}"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        timeout = httpx.Timeout(config.timeout_seconds, connect=min(5.0, config.timeout_seconds))
        # HTTPX read timeouts only bound idle time between chunks. This deadline
        # covers pool acquisition, connection, request write and the entire body.
        async with asyncio.timeout(config.timeout_seconds):
            self.traces.mark_forwarded(trace_id, upstream_model=config.model)
            async with self.client.stream("POST", url, json=payload, headers=headers, timeout=timeout) as response:
                request_id = response.headers.get("x-request-id", "")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_UPSTREAM_RESPONSE_BYTES:
                        raise UpstreamError("上游响应体过大", "upstream_response_too_large")
                    body.extend(chunk)
                self.traces.mark_llm_replied(trace_id, upstream_http_status=response.status_code,
                                             upstream_request_id=request_id, response_bytes=len(body))
                if not 200 <= response.status_code < 300:
                    LOGGER.warning("upstream failed status=%s", response.status_code)
                    raise UpstreamError(f"上游模型网关返回 HTTP {response.status_code}", "upstream_http_error")
        return body, request_id

    async def audit(self, text: str, *, trace_id: str = "", request_model: str = "",
                    input_bytes: int = 0) -> AuditCall:
        owns_trace = not trace_id
        if owns_trace:
            trace_id = self.traces.begin(source="internal", request_model=request_model)
        final_status = 500
        sent = True
        try:
            config = self.store.get()
            self.traces.update_request(trace_id, request_model=request_model, upstream_model=config.model,
                                       input_chars=len(text), input_bytes=input_bytes or len(text.encode("utf-8")))
            if self.store.load_error:
                raise UpstreamError("上游配置加载失败，请在管理页面修复配置", "invalid_upstream_config", status_code=503)
            if not config.ready:
                raise UpstreamError("审计服务尚未完成上游配置", "auditer_not_configured", status_code=503)
            max_chars = env_int("MAX_INPUT_CHARS", 200_000)
            if len(text) > max_chars:
                raise UpstreamError(f"待审核文本超过 {max_chars} 个字符", "audit_input_too_large", status_code=413)
            started = time.perf_counter()
            try:
                body, request_id = await self._upstream(config, text, trace_id)
            except (TimeoutError, httpx.TimeoutException) as exc:
                raise UpstreamError("调用上游模型超时", "upstream_timeout", status_code=504) from exc
            except httpx.RequestError as exc:
                raise UpstreamError("无法连接上游模型网关", "upstream_connection_error") from exc
            try:
                upstream = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UpstreamError("上游模型网关返回的不是有效 JSON", "upstream_invalid_json") from exc
            if not isinstance(upstream, Mapping):
                raise UpstreamError("上游模型网关返回格式无效", "upstream_invalid_envelope")
            try:
                raw = extract_upstream_content(upstream)
                # Deliberately preserve the existing permissive result parser.
                result = parse_model_result(raw)
            except ProtocolError as exc:
                raise UpstreamError(str(exc), "audit_model_invalid_response") from exc
            latency = max(0, int((time.perf_counter() - started) * 1000))
            self.traces.mark_result(trace_id, safety=result.safety, categories=result.categories)
            final_status = 200
            return AuditCall(result, raw, latency, request_id, trace_id)
        except UpstreamError as exc:
            final_status = exc.status_code
            self.traces.mark_error(trace_id, code=exc.code, message=str(exc), http_status=exc.status_code)
            raise
        except asyncio.CancelledError:
            final_status, sent = 499, False
            self.traces.mark_error(trace_id, code="audit_cancelled", message="审核任务被取消", http_status=499)
            raise
        except Exception:
            self.traces.mark_error(trace_id, code="internal_error", message="审计服务发生内部错误", http_status=500)
            raise
        finally:
            if owns_trace:
                pending = self.traces.finish(trace_id, http_status=final_status, sent=sent)
                await self.traces.persist_async(pending)
