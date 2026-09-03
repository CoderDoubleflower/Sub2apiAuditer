from __future__ import annotations

import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any


def _iso_now() -> tuple[str, int, float]:
    """Return a wall-clock timestamp plus monotonic and epoch clocks.

    Wall time is for display. Durations always use perf_counter_ns so NTP or
    clock adjustments cannot produce negative phase latencies.
    """

    now = datetime.now(timezone.utc)
    rendered = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return rendered, time.perf_counter_ns(), now.timestamp()


def _duration_ms(start_ns: int, end_ns: int) -> float | None:
    if start_ns <= 0 or end_ns <= 0 or end_ns < start_ns:
        return None
    return round((end_ns - start_ns) / 1_000_000, 3)


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 3)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


@dataclass(slots=True)
class ProcessingTrace:
    id: str
    source: str
    received_at: str
    received_perf_ns: int
    received_epoch: float
    client_request_id: str = ""
    request_model: str = ""
    upstream_model: str = ""
    input_chars: int = 0
    input_bytes: int = 0
    forwarded_at: str = ""
    forwarded_perf_ns: int = 0
    llm_replied_at: str = ""
    llm_replied_perf_ns: int = 0
    sub2api_replied_at: str = ""
    sub2api_replied_perf_ns: int = 0
    status: str = "processing"
    http_status: int = 0
    upstream_http_status: int = 0
    upstream_request_id: str = ""
    upstream_response_bytes: int = 0
    safety: str = ""
    categories: tuple[str, ...] = field(default_factory=tuple)
    error_code: str = ""
    error_message: str = ""

    def snapshot(self, *, now_perf_ns: int | None = None) -> dict[str, Any]:
        now_perf_ns = now_perf_ns or time.perf_counter_ns()
        preprocess_ms = _duration_ms(self.received_perf_ns, self.forwarded_perf_ns)
        upstream_ms = _duration_ms(self.forwarded_perf_ns, self.llm_replied_perf_ns)
        response_ms = _duration_ms(self.llm_replied_perf_ns, self.sub2api_replied_perf_ns)
        total_ms = _duration_ms(self.received_perf_ns, self.sub2api_replied_perf_ns)
        elapsed_ms = total_ms
        if elapsed_ms is None:
            elapsed_ms = _duration_ms(self.received_perf_ns, now_perf_ns)
        return {
            "id": self.id,
            "source": self.source,
            "received_at": self.received_at,
            "forwarded_at": self.forwarded_at or None,
            "llm_replied_at": self.llm_replied_at or None,
            "sub2api_replied_at": self.sub2api_replied_at or None,
            "client_request_id": self.client_request_id,
            "request_model": self.request_model,
            "upstream_model": self.upstream_model,
            "input_chars": self.input_chars,
            "input_bytes": self.input_bytes,
            "status": self.status,
            "http_status": self.http_status or None,
            "upstream_http_status": self.upstream_http_status or None,
            "upstream_request_id": self.upstream_request_id,
            "upstream_response_bytes": self.upstream_response_bytes,
            "safety": self.safety,
            "categories": list(self.categories),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "preprocess_ms": preprocess_ms,
            "upstream_ms": upstream_ms,
            "response_ms": response_ms,
            "total_ms": total_ms,
            "elapsed_ms": elapsed_ms,
        }


class TraceStore:
    """A lock-protected, in-memory ring buffer for the latest processing traces."""

    def __init__(self, capacity: int = 100) -> None:
        self.capacity = max(10, min(int(capacity), 100))
        self._items: deque[ProcessingTrace] = deque()
        self._by_id: dict[str, ProcessingTrace] = {}
        self._lock = RLock()

    def begin(
        self,
        *,
        source: str,
        client_request_id: str = "",
        request_model: str = "",
    ) -> str:
        rendered, perf_ns, epoch = _iso_now()
        trace = ProcessingTrace(
            id=f"aud-{uuid.uuid4().hex[:16]}",
            source=source,
            received_at=rendered,
            received_perf_ns=perf_ns,
            received_epoch=epoch,
            client_request_id=client_request_id[:256],
            request_model=request_model[:256],
        )
        with self._lock:
            if len(self._items) >= self.capacity:
                removed = self._items.popleft()
                self._by_id.pop(removed.id, None)
            self._items.append(trace)
            self._by_id[trace.id] = trace
        return trace.id

    def update_request(
        self,
        trace_id: str,
        *,
        request_model: str,
        upstream_model: str,
        input_chars: int,
        input_bytes: int,
    ) -> None:
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None:
                return
            trace.request_model = request_model[:256]
            trace.upstream_model = upstream_model[:256]
            trace.input_chars = max(0, int(input_chars))
            trace.input_bytes = max(0, int(input_bytes))

    def mark_forwarded(self, trace_id: str, *, upstream_model: str = "") -> None:
        rendered, perf_ns, _ = _iso_now()
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None or trace.forwarded_perf_ns:
                return
            trace.forwarded_at = rendered
            trace.forwarded_perf_ns = perf_ns
            if upstream_model:
                trace.upstream_model = upstream_model[:256]

    def mark_llm_replied(
        self,
        trace_id: str,
        *,
        upstream_http_status: int = 0,
        upstream_request_id: str = "",
        response_bytes: int = 0,
    ) -> None:
        rendered, perf_ns, _ = _iso_now()
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None:
                return
            if not trace.llm_replied_perf_ns:
                trace.llm_replied_at = rendered
                trace.llm_replied_perf_ns = perf_ns
            trace.upstream_http_status = max(0, int(upstream_http_status))
            trace.upstream_request_id = upstream_request_id[:256]
            trace.upstream_response_bytes = max(0, int(response_bytes))

    def mark_result(self, trace_id: str, *, safety: str, categories: tuple[str, ...]) -> None:
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None:
                return
            trace.status = "success"
            trace.safety = safety[:32]
            trace.categories = tuple(value[:80] for value in categories[:12])
            trace.error_code = ""
            trace.error_message = ""

    def mark_error(
        self,
        trace_id: str,
        *,
        code: str,
        message: str,
        http_status: int = 0,
    ) -> None:
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None:
                return
            trace.status = "error"
            trace.error_code = code[:128]
            trace.error_message = message[:1000]
            if http_status:
                trace.http_status = int(http_status)

    def mark_replied(self, trace_id: str, *, http_status: int) -> None:
        rendered, perf_ns, _ = _iso_now()
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None:
                return
            if not trace.sub2api_replied_perf_ns:
                trace.sub2api_replied_at = rendered
                trace.sub2api_replied_perf_ns = perf_ns
            trace.http_status = int(http_status)
            if trace.status == "processing":
                trace.status = "success" if 200 <= http_status < 400 else "error"

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), self.capacity))
        with self._lock:
            now_ns = time.perf_counter_ns()
            return [item.snapshot(now_perf_ns=now_ns) for item in list(self._items)[-limit:][::-1]]

    def clear(self) -> int:
        with self._lock:
            count = len(self._items)
            self._items.clear()
            self._by_id.clear()
            return count

    def runtime_stats(self) -> dict[str, Any]:
        snapshots = self.list(limit=self.capacity)
        successful = [item for item in snapshots if item["status"] == "success"]
        failed = [item for item in snapshots if item["status"] == "error"]
        in_flight = [item for item in snapshots if item["status"] == "processing"]
        last = snapshots[0] if snapshots else None
        last_completed = next((item for item in snapshots if item["total_ms"] is not None), None)
        return {
            "total": len(snapshots),
            "success": len(successful),
            "failed": len(failed),
            "in_flight": len(in_flight),
            "last_latency_ms": last_completed["total_ms"] if last_completed else 0,
            "last_error_code": next((item["error_code"] for item in snapshots if item["error_code"]), ""),
            "last_request_at": last["received_at"] if last else "",
            "capacity": self.capacity,
        }

    def statistics(self) -> dict[str, Any]:
        snapshots = self.list(limit=self.capacity)
        completed = [item for item in snapshots if item["total_ms"] is not None]
        successful = [item for item in snapshots if item["status"] == "success"]
        failed = [item for item in snapshots if item["status"] == "error"]
        processing = [item for item in snapshots if item["status"] == "processing"]

        total_values = [float(item["total_ms"]) for item in completed if item["total_ms"] is not None]
        preprocess_values = [float(item["preprocess_ms"]) for item in completed if item["preprocess_ms"] is not None]
        upstream_values = [float(item["upstream_ms"]) for item in completed if item["upstream_ms"] is not None]
        response_values = [float(item["response_ms"]) for item in completed if item["response_ms"] is not None]

        decision_counts = Counter((item["safety"] or "Unclassified") for item in snapshots)
        error_counts = Counter(item["error_code"] for item in failed if item["error_code"])
        now_epoch = time.time()
        with self._lock:
            rpm = sum(1 for item in self._items if item.received_epoch >= now_epoch - 60)

        chronological = list(reversed(completed[:30]))
        series = [
            {
                "id": item["id"],
                "label": item["received_at"][11:19] if item["received_at"] else "",
                "total_ms": item["total_ms"] or 0,
                "upstream_ms": item["upstream_ms"] or 0,
                "status": item["status"],
                "safety": item["safety"],
            }
            for item in chronological
        ]
        slowest = sorted(
            (item for item in completed if item["total_ms"] is not None),
            key=lambda item: float(item["total_ms"]),
            reverse=True,
        )[:5]

        success_rate = 0.0
        terminal = len(successful) + len(failed)
        if terminal:
            success_rate = round(len(successful) * 100 / terminal, 2)

        return {
            "generated_at": _iso_now()[0],
            "capacity": self.capacity,
            "window_size": len(snapshots),
            "completed": len(completed),
            "success": len(successful),
            "failed": len(failed),
            "in_flight": len(processing),
            "success_rate": success_rate,
            "rpm_1m": rpm,
            "latency": {
                "average_ms": _average(total_values),
                "p50_ms": _percentile(total_values, 0.50),
                "p95_ms": _percentile(total_values, 0.95),
                "maximum_ms": round(max(total_values), 3) if total_values else 0.0,
                "upstream_p95_ms": _percentile(upstream_values, 0.95),
            },
            "phases": {
                "preprocess_average_ms": _average(preprocess_values),
                "upstream_average_ms": _average(upstream_values),
                "response_average_ms": _average(response_values),
            },
            "decisions": {
                "Safe": decision_counts.get("Safe", 0),
                "Controversial": decision_counts.get("Controversial", 0),
                "Unsafe": decision_counts.get("Unsafe", 0),
                "Unclassified": decision_counts.get("Unclassified", 0),
            },
            "errors": [
                {"code": code, "count": count}
                for code, count in error_counts.most_common(8)
            ],
            "series": series,
            "slowest": [
                {
                    "id": item["id"],
                    "received_at": item["received_at"],
                    "total_ms": item["total_ms"],
                    "upstream_ms": item["upstream_ms"],
                    "status": item["status"],
                    "error_code": item["error_code"],
                }
                for item in slowest
            ],
            "window": {
                "oldest_at": snapshots[-1]["received_at"] if snapshots else None,
                "newest_at": snapshots[0]["received_at"] if snapshots else None,
            },
        }
