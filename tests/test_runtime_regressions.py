"""Regression coverage for the runtime fixes; result parsing policy is unchanged."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import asdict
from types import SimpleNamespace

import anyio
import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from sub2api_auditer.config import AuditConfig, ConfigStore
from sub2api_auditer.normalize import parse_model_result
from sub2api_auditer.observability import TraceStore
from sub2api_auditer.protocol import ProtocolError
from sub2api_auditer.service import AuditerService, UpstreamError
from sub2api_auditer.web import _traced_response, create_app, clear_processing_logs, _read_json


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for name in ["TRACE_DB_PATH", "UPSTREAM_BASE_URL", "UPSTREAM_API_KEY", "UPSTREAM_MODEL",
                 "UPSTREAM_TIMEOUT_SECONDS", "UPSTREAM_MAX_TOKENS", "AUDIT_PROMPT",
                 "ADMIN_TOKEN", "AUDITER_TOKEN", "LOG_CAPACITY", "MAX_REQUEST_BODY_BYTES",
                 "MAX_INPUT_CHARS", "BASE_PATH"]:
        monkeypatch.delenv(name, raising=False)


def _count(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT COUNT(*) FROM processing_traces").fetchone()[0]


def _complete(store):
    trace_id = store.begin(source="sub2api")
    store.mark_forwarded(trace_id)
    store.mark_llm_replied(trace_id, upstream_http_status=200)
    store.mark_result(trace_id, safety="Safe", categories=())
    store.mark_replied(trace_id, http_status=200)
    return trace_id


def _request(store):
    app = SimpleNamespace(state=SimpleNamespace(admin_token="", service=SimpleNamespace(traces=store)))
    return Request({"type": "http", "method": "DELETE", "path": "/api/logs", "headers": [], "app": app})






def test_completed_trace_not_evicted_before_response_callback(tmp_path):
    path = tmp_path / "audit.db"
    store = TraceStore(100, path)
    ids = [store.begin(source="sub2api") for _ in range(100)]
    store.mark_result(ids[0], safety="Safe", categories=())
    store.begin(source="sub2api")  # A new request arrives before the old callback.
    store.mark_replied(ids[0], http_status=200)
    assert _count(path) == 1, "A completed request was lost before SQLite persistence"


def test_clear_does_not_allow_pending_write_to_resurrect_logs(tmp_path, monkeypatch):
    path = tmp_path / "audit.db"
    store = TraceStore(100, path)
    trace_id = store.begin(source="sub2api")
    store.mark_result(trace_id, safety="Safe", categories=())
    captured, release = threading.Event(), threading.Event()
    original = store._persist_snapshot
    errors = []

    def delayed(snapshot, epoch):
        captured.set()
        if not release.wait(3):
            raise RuntimeError("review synchronization timed out")
        original(snapshot, epoch)

    def finish():
        try:
            store.mark_replied(trace_id, http_status=200)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(store, "_persist_snapshot", delayed)
    worker = threading.Thread(target=finish)
    worker.start()
    try:
        assert captured.wait(3)
        store.clear()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    assert _count(path) == 0, "A cleared trace was reinserted by an old callback"


def test_failed_database_clear_must_not_report_http_200(tmp_path, monkeypatch):
    path = tmp_path / "audit.db"
    store = TraceStore(100, path)
    _complete(store)

    def broken():
        raise sqlite3.OperationalError("read-only database (review simulation)")

    monkeypatch.setattr(store, "_connect", broken)
    response = asyncio.run(clear_processing_logs(_request(store)))
    assert response.status_code >= 500, (response.status_code, response.body, _count(path))


def test_connections_are_explicitly_closed_after_persistence(tmp_path, monkeypatch):
    store = TraceStore(100, tmp_path / "audit.db")
    opened = []
    original = store._connect

    def capture():
        connection = original()
        opened.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", capture)
    _complete(store)
    still_open = 0
    try:
        for connection in opened:
            try:
                connection.execute("SELECT 1")
                still_open += 1
            except sqlite3.ProgrammingError:
                pass
    finally:
        for connection in opened:
            connection.close()
    assert still_open == 0, f"{still_open} connection(s) remain usable after the write"


def test_normal_synchronous_is_applied_to_write_connections(tmp_path):
    store = TraceStore(100, tmp_path / "audit.db")
    with closing(store._connect()) as connection:
        actual = connection.execute("PRAGMA synchronous").fetchone()[0]
    assert actual == 1, f"synchronous={actual}; expected NORMAL=1"


def test_db_lock_does_not_block_event_loop_during_clear(tmp_path):
    path = tmp_path / "audit.db"
    store = TraceStore(100, path)
    _complete(store)
    locked = threading.Event()
    errors = []

    def locker():
        try:
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                locked.set()
                time.sleep(0.3)
                connection.rollback()
        except Exception as exc:
            errors.append(exc)
            locked.set()

    worker = threading.Thread(target=locker)
    worker.start()
    assert locked.wait(3)

    async def scenario():
        async def heartbeat():
            start = time.perf_counter()
            await asyncio.sleep(0.01)
            return (time.perf_counter() - start) * 1000
        probe = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        response = await clear_processing_logs(_request(store))
        assert response.status_code == 200
        return await probe

    try:
        latency = asyncio.run(scenario())
    finally:
        worker.join(3)
    assert not errors
    assert latency < 100, f"10ms event-loop heartbeat delayed to {latency:.1f}ms by SQLite"


def test_response_timestamp_does_not_include_thread_pool_wait():
    async def scenario():
        store = TraceStore(100)
        trace_id = store.begin(source="sub2api")
        store.mark_forwarded(trace_id)
        store.mark_llm_replied(trace_id, upstream_http_status=200)
        store.mark_result(trace_id, safety="Safe", categories=())
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        entered, release = threading.Event(), threading.Event()
        sent = asyncio.Event()
        sent_ns = 0

        def occupy():
            entered.set()
            release.wait(3)

        worker = asyncio.create_task(anyio.to_thread.run_sync(occupy))
        await asyncio.to_thread(entered.wait, 2)

        async def send(message):
            nonlocal sent_ns
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                sent_ns = time.perf_counter_ns()
                sent.set()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        response = _traced_response(JSONResponse({"ok": True}), SimpleNamespace(traces=store), trace_id)
        task = asyncio.create_task(response({"type": "http"}, receive, send))
        try:
            await asyncio.wait_for(sent.wait(), 2)
            await asyncio.sleep(0.15)
            release.set()
            await task
            return (store._by_id[trace_id].sub2api_replied_perf_ns - sent_ns) / 1_000_000
        finally:
            release.set()
            await worker
            limiter.total_tokens = previous

    delay = asyncio.run(scenario())
    assert delay < 50, f"send timestamp includes {delay:.1f}ms of thread-pool queueing"


def test_upstream_timeout_is_total_request_deadline(tmp_path):
    async def scenario():
        body = json.dumps({"choices": [{"message": {"content": '{"safety":"Safe","categories":[]}'}}]}).encode()
        handlers = set()

        async def serve(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                content_length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:"))
                await reader.readexactly(content_length)
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode())
                await writer.drain()
                # Every chunk arrives within the 1s idle timeout, but total >1s.
                for index in range(4):
                    await asyncio.sleep(0.35)
                    writer.write(body[len(body) * index // 4:len(body) * (index + 1) // 4])
                    await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        store = ConfigStore(tmp_path / "config.json")
        store._config = AuditConfig(base_url=f"http://127.0.0.1:{port}", model="review", timeout_seconds=1.0)
        try:
            async with server, httpx.AsyncClient(trust_env=False) as client:
                service = AuditerService(store, client, TraceStore())
                start = time.perf_counter()
                try:
                    await service.audit("test")
                except UpstreamError as exc:
                    assert exc.code == "upstream_timeout"
                    return
                pytest.fail(f"configured timeout=1.0s but request succeeded after {time.perf_counter()-start:.3f}s")
        finally:
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.gather(*list(handlers), return_exceptions=True)

    asyncio.run(scenario())


def test_valid_file_takes_precedence_over_invalid_environment(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(asdict(AuditConfig(base_url="https://gateway.invalid/v1", model="review"))), encoding="utf-8")
    monkeypatch.setenv("UPSTREAM_TIMEOUT_SECONDS", "0")
    store = ConfigStore(path)
    asyncio.run(store.load())
    assert store.get().timeout_seconds == 20


def test_invalid_file_does_not_silently_send_content_to_env_upstream(tmp_path, monkeypatch):
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://old-gateway.invalid/v1")
    monkeypatch.setenv("UPSTREAM_MODEL", "old-review-model")
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"safety":"Safe","categories":[]}'}}]})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(config_path=str(path), client=upstream, admin_token="", auditer_token="")
    try:
        with TestClient(app) as client:
            assert client.get("/readyz").status_code == 503
            response = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "review test"}]})
            assert response.status_code == 503 and not requests, (response.status_code, [str(r.url) for r in requests])
    finally:
        asyncio.run(upstream.aclose())


def test_non_ascii_authorization_rejected_not_internal_error(tmp_path):
    app = create_app(config_path=str(tmp_path / "config.json"), admin_token="test-admin", auditer_token="test-auditer")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/chat/completions", headers={b"Authorization": b"Bearer \xff"}, json={"messages": [{"role": "user", "content": "test"}]})
        stats = app.state.service.traces.runtime_stats()
        assert response.status_code == 401 and stats["in_flight"] == 0, (response.status_code, stats)


def test_oversized_content_length_is_rejected_before_reading(monkeypatch):
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "10")
    read_calls = []

    async def receive():
        read_calls.append(1)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request({"type": "http", "headers": [(b"content-length", b"100")]}, receive)
    with pytest.raises(ProtocolError):
        asyncio.run(_read_json(request))
    assert not read_calls


def test_standard_json_and_restart_are_still_working(tmp_path):
    result = parse_model_result('{"safety":"Unsafe","categories":["Jailbreak"]}')
    assert result.sub2api_content() == "Safety: Unsafe\nCategories: Jailbreak"
    path = tmp_path / "audit.db"
    store = TraceStore(100, path)
    for _ in range(105):
        _complete(store)
    assert _count(path) == 100
    restored = TraceStore(100, path)
    assert len(restored.list()) == 100
    assert restored.statistics()["completed"] == 100
