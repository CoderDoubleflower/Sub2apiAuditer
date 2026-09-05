"""Integration tests for completion, cancellation, persistence and mounted routes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from sub2api_auditer.config import ConfigError, ConfigStore
from sub2api_auditer.observability import TraceStore
from sub2api_auditer.web import _traced_response, create_app


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("TRACE_DB_PATH", "BASE_PATH", "UPSTREAM_BASE_URL", "UPSTREAM_API_KEY", "UPSTREAM_MODEL",
                 "UPSTREAM_TIMEOUT_SECONDS", "UPSTREAM_MAX_TOKENS", "AUDIT_PROMPT", "ADMIN_TOKEN", "AUDITER_TOKEN",
                 "MAX_REQUEST_BODY_BYTES", "LOG_CAPACITY"):
        monkeypatch.delenv(name, raising=False)


def _response(request):
    return httpx.Response(200, json={"choices": [{"message": {"content": 'Safety: Safe\nCategories: None'}}]})


async def _configure(app):
    await app.state.store.update({"base_url": "https://gateway.invalid/v1", "model": "audit", "prompt": "policy"})


def test_parser_and_output_contract_are_byte_identical_to_120():
    # The owner explicitly declined the stricter parser changes from the review.
    root = Path(__file__).resolve().parents[1] / "src/sub2api_auditer"
    for name, expected in {"normalize.py": "5cdf1fa685c46b510981034c59a5fb131c7edde1",
                           "protocol.py": "1e435f18229a30fd25e1384d2f199eca213f91a8"}.items():
        data = (root / name).read_bytes()
        assert hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() == expected


def test_verdict_is_not_a_finished_response():
    store = TraceStore()
    trace_id = store.begin(source="sub2api")
    store.mark_result(trace_id, safety="Safe", categories=())
    assert store.list()[0]["status"] == "processing"
    assert store.statistics()["completed"] == 0
    assert store.runtime_stats()["in_flight"] == 1
    store.mark_replied(trace_id, http_status=200)
    assert store.runtime_stats()["in_flight"] == 0


def test_completed_window_survives_large_inflight_burst(tmp_path):
    store = TraceStore(100, tmp_path / "traces.db")
    for _ in range(100):
        store.mark_replied(store.begin(source="sub2api"), http_status=200)
    for _ in range(150):
        store.begin(source="sub2api")
    assert sum(trace.finished for trace in store._items) == 100
    assert store.runtime_stats()["in_flight"] == 150
    assert len(store.list()) == 100


def test_concurrent_completions_all_finish_and_keep_100(tmp_path):
    path = tmp_path / "traces.db"
    store = TraceStore(100, path)
    ids = [store.begin(source="sub2api") for _ in range(200)]
    for trace_id in ids:
        store.mark_result(trace_id, safety="Safe", categories=())
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda trace_id: store.mark_replied(trace_id, http_status=200), reversed(ids)))
    assert store.runtime_stats()["in_flight"] == 0
    restored = TraceStore(100, path)
    assert [item["id"] for item in restored.list()] == list(reversed(ids[-100:]))
    assert [item["id"] for item in store.list()] == [item["id"] for item in restored.list()]


def test_duplicate_response_callback_is_idempotent(tmp_path):
    store = TraceStore(100, tmp_path / "traces.db")
    trace_id = store.begin(source="sub2api")
    store.mark_replied(trace_id, http_status=200)
    original = store.list()[0]
    store.mark_replied(trace_id, http_status=500)
    assert store.list()[0] == original


def test_clear_fences_old_snapshots_but_preserves_new_requests(tmp_path):
    path = tmp_path / "traces.db"
    store = TraceStore(100, path)
    old = store.finish(store.begin(source="sub2api"), http_status=200)
    store.clear()
    new_id = store.begin(source="sub2api")
    store.mark_replied(new_id, http_status=200)
    store._persist_snapshot(*old)
    assert [item["id"] for item in TraceStore(100, path).list()] == [new_id]


def test_failed_clear_keeps_memory_and_generation(tmp_path, monkeypatch):
    store = TraceStore(100, tmp_path / "traces.db")
    trace_id = store.begin(source="sub2api")
    pending = store.finish(trace_id, http_status=200)
    original = store._connect
    def broken():
        raise sqlite3.OperationalError("simulated failure")
    monkeypatch.setattr(store, "_connect", broken)
    with pytest.raises(RuntimeError):
        store.clear()
    assert store.list()[0]["id"] == trace_id
    monkeypatch.setattr(store, "_connect", original)
    store._persist_snapshot(*pending)
    assert len(TraceStore(100, store.db_path).list()) == 1


def test_failed_write_is_visible_in_runtime_status(tmp_path, monkeypatch):
    store = TraceStore(100, tmp_path / "traces.db")
    def broken():
        raise sqlite3.OperationalError("simulated disk failure")
    monkeypatch.setattr(store, "_connect", broken)
    store.mark_replied(store.begin(source="sub2api"), http_status=200)
    assert store.runtime_stats()["persistence_error"]


async def test_cancelled_audit_finishes_and_persists_without_sent_timestamp(tmp_path, monkeypatch):
    path = tmp_path / "traces.db"
    monkeypatch.setenv("TRACE_DB_PATH", str(path))
    entered = asyncio.Event()
    async def upstream_handler(request):
        entered.set()
        await asyncio.Future()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler)) as upstream:
        app = create_app(config_path=str(tmp_path / "config.json"), client=upstream)
        async with app.router.lifespan_context(app):
            await _configure(app)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                task = asyncio.create_task(client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "test"}]}))
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert app.state.service.traces.runtime_stats()["in_flight"] == 0
    trace = TraceStore(100, path).list()[0]
    assert trace["status"] == "error" and trace["error_code"] == "audit_cancelled"
    assert trace["http_status"] == 499
    assert trace["sub2api_replied_at"] is None and trace["total_ms"] is not None


async def test_send_failure_is_not_recorded_as_sent(tmp_path):
    store = TraceStore(100, tmp_path / "traces.db")
    trace_id = store.begin(source="sub2api")
    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("simulated disconnect")
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    response = _traced_response(JSONResponse({"ok": True}), SimpleNamespace(traces=store), trace_id)
    with pytest.raises(OSError):
        await response({"type": "http"}, receive, send)
    restored = TraceStore(100, store.db_path).list()[0]
    assert restored["error_code"] == "response_send_failed" and restored["status"] == "error"
    assert restored["http_status"] == 500 and restored["sub2api_replied_at"] is None


async def test_disconnect_while_reading_body_finishes_trace(tmp_path):
    app = create_app(config_path=str(tmp_path / "config.json"))
    async with app.router.lifespan_context(app):
        scope = {"type": "http", "asgi": {"version": "3.0"}, "method": "POST", "path": "/v1/chat/completions",
                 "root_path": "", "query_string": b"", "headers": [], "scheme": "http", "server": ("test", 80), "client": ("test", 123)}
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            pytest.fail("A disconnected client must not get a manufactured response")
        await app(scope, receive, send)
        trace = app.state.service.traces.list()[0]
        assert trace["error_code"] == "client_disconnected" and trace["status"] == "error"
        assert trace["sub2api_replied_at"] is None


@pytest.mark.parametrize("prefix", ["", "/auditer", "/tools/auditer"])
def test_mounted_api_and_static_javascript(tmp_path, prefix):
    app = create_app(config_path=str(tmp_path / "config.json"), base_path=prefix, admin_token="admin")
    with TestClient(app) as client:
        if prefix:
            redirect = client.get(prefix, follow_redirects=False)
            assert redirect.status_code in (307, 308)
            assert redirect.headers["location"].endswith(prefix + "/")
        home = client.get(prefix + "/")
        assert home.status_code == 200 and './assets/app.js' in home.text
        assert client.get(prefix + "/assets/app.js").status_code == 200
        assert client.get(prefix + "/api/status", headers={"Authorization": "Bearer admin"}).status_code == 200
        assert client.get("/healthz").status_code == 200
        if prefix:
            assert client.get("/api/status").status_code == 404


@pytest.mark.parametrize("path", ["/api/config", "/api/status", "/api/logs", "/api/statistics", "/v1/models"])
def test_malformed_auth_is_401_on_all_protected_read_routes(tmp_path, path):
    app = create_app(config_path=str(tmp_path / "config.json"), admin_token="a", auditer_token="b")
    with TestClient(app) as client:
        response = client.get(path, headers={b"Authorization": b"Bearer \xff"})
        assert response.status_code == 401


def test_invalid_base_path_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        create_app(config_path=str(tmp_path / "c.json"), base_path="//evil.invalid/a")


async def test_bad_env_is_reported_but_management_can_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("UPSTREAM_TIMEOUT_SECONDS", "0")
    app = create_app(config_path=str(tmp_path / "config.json"))
    async with app.router.lifespan_context(app):
        assert not app.state.store.ready and app.state.store.load_error
        await _configure(app)
        assert app.state.store.ready and not app.state.store.load_error


async def test_slow_database_write_does_not_block_event_loop(tmp_path, monkeypatch):
    store = TraceStore(100, tmp_path / "traces.db")
    pending = store.finish(store.begin(source="sub2api"), http_status=200)
    original = store._persist_snapshot
    def slow(*args):
        time.sleep(.15)
        original(*args)
    monkeypatch.setattr(store, "_persist_snapshot", slow)
    writer = asyncio.create_task(store.persist_async(pending))
    start = time.perf_counter()
    await asyncio.sleep(.01)
    assert time.perf_counter() - start < .1
    await writer
