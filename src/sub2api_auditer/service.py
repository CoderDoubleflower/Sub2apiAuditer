from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from . import __version__
from .config import ConfigStore
from .normalize import parse_model_result
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


@dataclass(slots=True)
class RuntimeStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    in_flight: int = 0
    last_latency_ms: int = 0
    last_error_code: str = ""
    last_request_at: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "in_flight": self.in_flight,
            "last_latency_ms": self.last_latency_ms,
            "last_error_code": self.last_error_code,
            "last_request_at": self.last_request_at,
        }


@dataclass(frozen=True, slots=True)
class AuditCall:
    result: NormalizedAuditResult
    raw_output: str
    latency_ms: int
    upstream_request_id: str


class UpstreamError(RuntimeError):
    def __init__(self, message: str, code: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class AuditerService:
    def __init__(self, store: ConfigStore, client: httpx.AsyncClient) -> None:
        self.store = store
        self.client = client
        self.stats = RuntimeStats()

    async def audit(self, text: str) -> AuditCall:
        config = self.store.get()
        if not config.ready:
            raise UpstreamError("审计服务尚未完成上游配置", "auditer_not_configured", status_code=503)
        max_chars = env_int("MAX_INPUT_CHARS", 200_000)
        if len(text) > max_chars:
            raise UpstreamError(f"待审核文本超过 {max_chars} 个字符", "audit_input_too_large", status_code=413)

        url = build_chat_completions_url(config.base_url)
        payload = build_upstream_payload(model=config.model, prompt=config.prompt, text=text, max_tokens=config.max_tokens)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"Sub2apiAuditer/{__version__}",
        }
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        timeout = httpx.Timeout(config.timeout_seconds, connect=min(5.0, config.timeout_seconds))
        started = time.perf_counter()
        self.stats.total += 1
        self.stats.in_flight += 1
        self.stats.last_request_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            try:
                async with self.client.stream("POST", url, json=payload, headers=headers, timeout=timeout) as response:
                    request_id = response.headers.get("x-request-id", "")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_UPSTREAM_RESPONSE_BYTES:
                            raise UpstreamError("上游响应体过大", "upstream_response_too_large")
                        body.extend(chunk)
                    if not 200 <= response.status_code < 300:
                        LOGGER.warning("upstream failed status=%s request_id=%s", response.status_code, request_id or "-")
                        raise UpstreamError(f"上游模型网关返回 HTTP {response.status_code}", "upstream_http_error")
            except httpx.TimeoutException as exc:
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
                result = parse_model_result(raw)
            except ProtocolError as exc:
                raise UpstreamError(str(exc), "audit_model_invalid_response") from exc

            latency = max(0, int((time.perf_counter() - started) * 1000))
            self.stats.success += 1
            self.stats.last_latency_ms = latency
            self.stats.last_error_code = ""
            return AuditCall(result, raw, latency, request_id)
        except UpstreamError as exc:
            self.stats.failed += 1
            self.stats.last_error_code = exc.code
            self.stats.last_latency_ms = max(0, int((time.perf_counter() - started) * 1000))
            raise
        finally:
            self.stats.in_flight = max(0, self.stats.in_flight - 1)
