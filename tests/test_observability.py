from __future__ import annotations

import time

from sub2api_auditer.observability import TraceStore


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
