from __future__ import annotations

import sqlite3
import time

from sub2api_auditer.observability import TraceStore


def _complete(store: TraceStore, *, client_request_id: str = "") -> str:
    trace_id = store.begin(
        source="sub2api",
        client_request_id=client_request_id,
        request_model="requested",
    )
    store.update_request(
        trace_id,
        request_model="requested",
        upstream_model="actual",
        input_chars=4,
        input_bytes=4,
    )
    store.mark_forwarded(trace_id)
    store.mark_llm_replied(
        trace_id,
        upstream_http_status=200,
        upstream_request_id="upstream-id",
        response_bytes=128,
    )
    store.mark_result(trace_id, safety="Unsafe", categories=("Jailbreak",))
    store.mark_replied(trace_id, http_status=200)
    return trace_id


def test_trace_store_keeps_latest_capacity_items():
    store = TraceStore(capacity=100)
    ids = [store.begin(source="sub2api", client_request_id=str(index)) for index in range(105)]
    items = store.list(limit=100)

    assert len(items) == 100
    assert items[0]["id"] == ids[-1]
    assert items[-1]["id"] == ids[5]


def test_trace_store_records_monotonic_phase_durations_and_statistics():
    store = TraceStore(capacity=100)
    trace_id = store.begin(source="sub2api", request_model="requested")
    store.update_request(
        trace_id,
        request_model="requested",
        upstream_model="actual",
        input_chars=4,
        input_bytes=4,
    )
    time.sleep(0.001)
    store.mark_forwarded(trace_id)
    time.sleep(0.001)
    store.mark_llm_replied(
        trace_id,
        upstream_http_status=200,
        upstream_request_id="upstream-id",
        response_bytes=128,
    )
    store.mark_result(trace_id, safety="Unsafe", categories=("Jailbreak",))
    time.sleep(0.001)
    store.mark_replied(trace_id, http_status=200)

    trace = store.list()[0]
    assert trace["preprocess_ms"] > 0
    assert trace["upstream_ms"] > 0
    assert trace["response_ms"] > 0
    assert trace["total_ms"] >= trace["preprocess_ms"] + trace["upstream_ms"]
    assert trace["upstream_model"] == "actual"
    assert trace["upstream_request_id"] == "upstream-id"

    stats = store.statistics()
    assert stats["window_size"] == 1
    assert stats["success"] == 1
    assert stats["decisions"]["Unsafe"] == 1
    assert stats["latency"]["p95_ms"] == trace["total_ms"]
    assert stats["phases"]["upstream_average_ms"] == trace["upstream_ms"]
    assert stats["persistence"] == "memory"


def test_sqlite_persistence_restores_completed_trace(tmp_path):
    db_path = tmp_path / "auditer.db"
    store = TraceStore(capacity=100, db_path=db_path)
    trace_id = _complete(store)

    assert db_path.exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    restored = TraceStore(capacity=100, db_path=db_path)
    items = restored.list()

    assert len(items) == 1
    assert items[0]["id"] == trace_id
    assert items[0]["status"] == "success"
    assert items[0]["safety"] == "Unsafe"
    assert items[0]["categories"] == ["Jailbreak"]
    assert items[0]["upstream_request_id"] == "upstream-id"
    assert items[0]["total_ms"] is not None
    assert restored.statistics()["persistence"] == "sqlite"


def test_sqlite_persistence_keeps_only_latest_100(tmp_path):
    db_path = tmp_path / "auditer.db"
    store = TraceStore(capacity=100, db_path=db_path)

    ids = []
    for index in range(105):
        ids.append(_complete(store, client_request_id=str(index)))

    restored = TraceStore(capacity=100, db_path=db_path)
    items = restored.list(limit=100)

    assert len(items) == 100
    assert items[0]["id"] == ids[-1]
    assert items[-1]["id"] == ids[5]

    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM processing_traces").fetchone()[0]
    assert count == 100


def test_clear_removes_persisted_traces(tmp_path):
    db_path = tmp_path / "auditer.db"
    store = TraceStore(capacity=100, db_path=db_path)
    _complete(store)
    _complete(store)

    assert store.clear() == 2
    assert store.list() == []

    restored = TraceStore(capacity=100, db_path=db_path)
    assert restored.list() == []
