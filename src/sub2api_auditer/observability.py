from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from collections import Counter, deque
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any

LOGGER = logging.getLogger("sub2api_auditer")
_SCHEMA_VERSION = 1
PendingTrace = tuple[dict[str, Any], float]


class TracePersistenceError(RuntimeError):
    """An explicit persistence operation failed; callers must not report success."""


def _iso_now() -> tuple[str, int, float]:
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z"), time.perf_counter_ns(), now.timestamp()


def _duration_ms(start_ns: int, end_ns: int) -> float | None:
    if start_ns <= 0 or end_ns <= 0 or end_ns < start_ns:
        return None
    return round((end_ns - start_ns) / 1_000_000, 3)


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)


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
    upstream_http_status: int = 0
    http_status: int = 0
    upstream_request_id: str = ""
    upstream_response_bytes: int = 0
    safety: str = ""
    categories: tuple[str, ...] = field(default_factory=tuple)
    error_code: str = ""
    error_message: str = ""
    persisted_preprocess_ms: float | None = None
    persisted_upstream_ms: float | None = None
    persisted_response_ms: float | None = None
    persisted_total_ms: float | None = None
    finished: bool = False

    def snapshot(self, *, now_perf_ns: int | None = None) -> dict[str, Any]:
        now_perf_ns = now_perf_ns or time.perf_counter_ns()
        preprocess_ms = (_duration_ms(self.received_perf_ns, self.forwarded_perf_ns)
                         if self.forwarded_perf_ns else self.persisted_preprocess_ms)
        upstream_ms = (_duration_ms(self.forwarded_perf_ns, self.llm_replied_perf_ns)
                       if self.forwarded_perf_ns and self.llm_replied_perf_ns else self.persisted_upstream_ms)
        response_ms = (_duration_ms(self.llm_replied_perf_ns, self.sub2api_replied_perf_ns)
                       if self.llm_replied_perf_ns and self.sub2api_replied_perf_ns else self.persisted_response_ms)
        total_ms = (_duration_ms(self.received_perf_ns, self.sub2api_replied_perf_ns)
                    if self.received_perf_ns and self.sub2api_replied_perf_ns else self.persisted_total_ms)
        elapsed_ms = total_ms
        if elapsed_ms is None and self.received_perf_ns:
            elapsed_ms = _duration_ms(self.received_perf_ns, now_perf_ns)
        return {
            "id": self.id, "source": self.source, "received_at": self.received_at,
            "forwarded_at": self.forwarded_at or None, "llm_replied_at": self.llm_replied_at or None,
            "sub2api_replied_at": self.sub2api_replied_at or None,
            "client_request_id": self.client_request_id, "request_model": self.request_model,
            "upstream_model": self.upstream_model, "input_chars": self.input_chars,
            "input_bytes": self.input_bytes, "status": self.status,
            "http_status": self.http_status or None, "upstream_http_status": self.upstream_http_status or None,
            "upstream_request_id": self.upstream_request_id, "upstream_response_bytes": self.upstream_response_bytes,
            "safety": self.safety, "categories": list(self.categories), "error_code": self.error_code,
            "error_message": self.error_message, "preprocess_ms": preprocess_ms, "upstream_ms": upstream_ms,
            "response_ms": response_ms, "total_ms": total_ms, "elapsed_ms": elapsed_ms,
        }


class TraceStore:
    """100 completed records plus active traces; only completed snapshots touch disk.

    The memory lock never covers disk I/O. Database writes and clear operations
    share a separate lock; a generation fence prevents pre-clear snapshots from
    reappearing afterwards. HTTP code calls finish() at send completion and then
    persist_async(), rather than capturing timestamps in a worker thread.
    """

    def __init__(self, capacity: int = 100, db_path: str | Path | None = None) -> None:
        self.capacity = max(10, min(int(capacity), 100))
        if db_path is None:
            db_path = os.getenv("TRACE_DB_PATH", "").strip() or None
        self.db_path = Path(db_path) if db_path else None
        self._items: deque[ProcessingTrace] = deque()
        self._by_id: dict[str, ProcessingTrace] = {}
        self._lock = RLock()
        self._db_lock = Lock()
        self._generation = 0
        self.persistence_error = ""
        if self.db_path is not None:
            self._initialize_database()
            self._restore_latest()

    @property
    def persistence(self) -> str:
        return "sqlite" if self.db_path is not None else "memory"

    def _connect(self) -> sqlite3.Connection:
        assert self.db_path is not None
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = NORMAL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _initialize_database(self) -> None:
        assert self.db_path is not None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS processing_traces (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL,
                    received_at TEXT NOT NULL, received_epoch REAL NOT NULL,
                    client_request_id TEXT NOT NULL DEFAULT '', request_model TEXT NOT NULL DEFAULT '',
                    upstream_model TEXT NOT NULL DEFAULT '', input_chars INTEGER NOT NULL DEFAULT 0,
                    input_bytes INTEGER NOT NULL DEFAULT 0, forwarded_at TEXT, llm_replied_at TEXT,
                    sub2api_replied_at TEXT NOT NULL, status TEXT NOT NULL,
                    http_status INTEGER NOT NULL DEFAULT 0, upstream_http_status INTEGER NOT NULL DEFAULT 0,
                    upstream_request_id TEXT NOT NULL DEFAULT '', upstream_response_bytes INTEGER NOT NULL DEFAULT 0,
                    safety TEXT NOT NULL DEFAULT '', categories_json TEXT NOT NULL DEFAULT '[]',
                    error_code TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '',
                    preprocess_ms REAL, upstream_ms REAL, response_ms REAL, total_ms REAL NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_processing_traces_received_epoch ON processing_traces(received_epoch DESC)")
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            self._prune_database(connection)

    def _prune_database(self, connection: sqlite3.Connection) -> None:
        connection.execute("""
            DELETE FROM processing_traces WHERE id NOT IN (
                SELECT id FROM processing_traces
                ORDER BY received_epoch DESC, sub2api_replied_at DESC LIMIT ?
            )
        """, (self.capacity,))

    def _restore_latest(self) -> None:
        if self.db_path is None:
            return
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT * FROM processing_traces
                ORDER BY received_epoch DESC, sub2api_replied_at DESC LIMIT ?
            """, (self.capacity,)).fetchall()
        for row in reversed(rows):
            values = dict(row)
            try:
                categories = json.loads(values.pop("categories_json") or "[]")
            except (ValueError, TypeError):
                categories = []
            if not isinstance(categories, list):
                categories = []
            values["categories"] = tuple(str(value)[:80] for value in categories
                                         if isinstance(value, (str, int, float)))[:12]
            for key in ("preprocess_ms", "upstream_ms", "response_ms", "total_ms"):
                values[f"persisted_{key}"] = values.pop(key)
            for key in ("forwarded_at", "llm_replied_at", "sub2api_replied_at"):
                values[key] = values[key] or ""
            trace = ProcessingTrace(**values, received_perf_ns=0, finished=True)
            self._items.append(trace)
            self._by_id[trace.id] = trace

    def _trim_memory_locked(self) -> None:
        completed = [item for item in self._items if item.finished]
        for trace in completed[:max(0, len(completed) - self.capacity)]:
            self._items.remove(trace)
            self._by_id.pop(trace.id, None)

    def _persist_snapshot(self, snapshot: dict[str, Any], received_epoch: float) -> None:
        if self.db_path is None:
            return
        with self._db_lock:
            with self._lock:
                if snapshot["_generation"] != self._generation:
                    return
            values = {key: value for key, value in snapshot.items()
                      if key not in {"_generation", "elapsed_ms", "categories"}}
            values["received_epoch"] = received_epoch
            values["categories_json"] = json.dumps(snapshot["categories"], ensure_ascii=False)
            # Empty response timestamp means the request was cancelled/send failed,
            # not that sub2api received a successful response. Schema v1 permits ''.
            values["sub2api_replied_at"] = values["sub2api_replied_at"] or ""
            values["http_status"] = values["http_status"] or 0
            values["upstream_http_status"] = values["upstream_http_status"] or 0
            columns = ", ".join(values)  # Keys are internal, never user-controlled.
            placeholders = ", ".join("?" for _ in values)
            try:
                with closing(self._connect()) as connection, connection:
                    connection.execute(f"INSERT OR REPLACE INTO processing_traces ({columns}) VALUES ({placeholders})", tuple(values.values()))
                    self._prune_database(connection)
                with self._lock:
                    self.persistence_error = ""
            except sqlite3.Error:
                with self._lock:
                    self.persistence_error = "日志写入数据库失败，请检查磁盘、权限和服务日志"
                LOGGER.exception("failed to persist processing trace trace_id=%s", snapshot["id"])

    async def persist_async(self, pending: PendingTrace | None) -> None:
        if pending is None or self.db_path is None:
            return
        task = asyncio.create_task(asyncio.to_thread(self._persist_snapshot, *pending))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not abandon an in-progress SQLite transaction on graceful shutdown.
            await task
            raise

    def begin(self, *, source: str, client_request_id: str = "", request_model: str = "") -> str:
        rendered, perf_ns, epoch = _iso_now()
        trace = ProcessingTrace(id=f"aud-{uuid.uuid4().hex[:16]}", source=source,
                                received_at=rendered, received_perf_ns=perf_ns, received_epoch=epoch,
                                client_request_id=client_request_id[:256], request_model=request_model[:256])
        with self._lock:
            self._items.append(trace)
            self._by_id[trace.id] = trace
        return trace.id

    def update_request(self, trace_id: str, *, request_model: str, upstream_model: str,
                       input_chars: int, input_bytes: int) -> None:
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None or trace.finished:
                return
            trace.request_model, trace.upstream_model = request_model[:256], upstream_model[:256]
            trace.input_chars, trace.input_bytes = max(0, int(input_chars)), max(0, int(input_bytes))

    def mark_forwarded(self, trace_id: str, *, upstream_model: str = "") -> None:
        rendered, perf_ns, _ = _iso_now()
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None or trace.finished or trace.forwarded_perf_ns:
                return
            trace.forwarded_at, trace.forwarded_perf_ns = rendered, perf_ns
            if upstream_model:
                trace.upstream_model = upstream_model[:256]

    def mark_llm_replied(self, trace_id: str, *, upstream_http_status: int = 0,
                         upstream_request_id: str = "", response_bytes: int = 0) -> None:
        rendered, perf_ns, _ = _iso_now()
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None or trace.finished:
                return
            if not trace.llm_replied_perf_ns:
                trace.llm_replied_at, trace.llm_replied_perf_ns = rendered, perf_ns
            trace.upstream_http_status = max(0, int(upstream_http_status))
            trace.upstream_request_id = upstream_request_id[:256]
            trace.upstream_response_bytes = max(0, int(response_bytes))

    def mark_result(self, trace_id: str, *, safety: str, categories: tuple[str, ...]) -> None:
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None or trace.finished:
                return
            # Having a verdict does not mean the response lifecycle has finished.
            trace.safety, trace.categories = safety[:32], tuple(value[:80] for value in categories[:12])
            trace.error_code = trace.error_message = ""

    def mark_error(self, trace_id: str, *, code: str, message: str, http_status: int = 0) -> None:
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None or trace.finished:
                return
            trace.error_code, trace.error_message = code[:128], message[:1000]
            if http_status:
                trace.http_status = int(http_status)

    def finish(self, trace_id: str, *, http_status: int, sent: bool = True) -> PendingTrace | None:
        """Capture completion immediately. This method never performs disk I/O."""
        rendered, perf_ns, _ = _iso_now()
        with self._lock:
            trace = self._by_id.get(trace_id)
            if trace is None or trace.finished:
                return None
            if sent:
                trace.sub2api_replied_at, trace.sub2api_replied_perf_ns = rendered, perf_ns
            else:
                trace.persisted_total_ms = _duration_ms(trace.received_perf_ns, perf_ns)
            trace.http_status = int(http_status)
            trace.status = "success" if sent and not trace.error_code and 200 <= http_status < 400 else "error"
            trace.finished = True
            snapshot = trace.snapshot(now_perf_ns=perf_ns)
            snapshot["_generation"] = self._generation
            self._trim_memory_locked()
            return snapshot, trace.received_epoch

    def mark_replied(self, trace_id: str, *, http_status: int) -> None:
        """Synchronous convenience for non-ASGI callers and tests."""
        pending = self.finish(trace_id, http_status=http_status)
        if pending is not None:
            self._persist_snapshot(*pending)

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), self.capacity))
        with self._lock:
            now_ns = time.perf_counter_ns()
            return [item.snapshot(now_perf_ns=now_ns) for item in list(self._items)[-limit:][::-1]]

    def clear(self) -> int:
        # Lock order: database -> memory. Never wait for SQLite with memory locked.
        with self._db_lock:
            database_count = 0
            if self.db_path is not None:
                try:
                    with closing(self._connect()) as connection, connection:
                        connection.execute("BEGIN IMMEDIATE")
                        database_count = connection.execute("SELECT COUNT(*) FROM processing_traces").fetchone()[0]
                        connection.execute("DELETE FROM processing_traces")
                except sqlite3.Error as exc:
                    with self._lock:
                        self.persistence_error = "清空日志数据库失败，请检查磁盘、权限和服务日志"
                    LOGGER.exception("failed to clear processing trace database")
                    raise TracePersistenceError(self.persistence_error) from exc
            # Linearization point: old callbacks become stale only after commit.
            with self._lock:
                count = len(self._items)
                self._generation += 1
                self._items.clear()
                self._by_id.clear()
                self.persistence_error = ""
            return max(count, database_count)

    def runtime_stats(self) -> dict[str, Any]:
        snapshots = self.list(limit=self.capacity)
        last_completed = next((item for item in snapshots if item["total_ms"] is not None), None)
        with self._lock:
            in_flight = sum(not trace.finished for trace in self._items)
            persistence_error = self.persistence_error
        return {
            "total": len(snapshots), "success": sum(item["status"] == "success" for item in snapshots),
            "failed": sum(item["status"] == "error" for item in snapshots), "in_flight": in_flight,
            "last_latency_ms": last_completed["total_ms"] if last_completed else 0,
            "last_error_code": next((item["error_code"] for item in snapshots if item["error_code"]), ""),
            "last_request_at": snapshots[0]["received_at"] if snapshots else "",
            "capacity": self.capacity, "persistence": self.persistence, "persistence_error": persistence_error,
        }

    def statistics(self) -> dict[str, Any]:
        snapshots = self.list(limit=self.capacity)
        completed = [item for item in snapshots if item["total_ms"] is not None]
        successful = [item for item in completed if item["status"] == "success"]
        failed = [item for item in completed if item["status"] == "error"]
        def values(key: str) -> list[float]:
            return [float(item[key]) for item in completed if item[key] is not None]
        total_values, upstream_values = values("total_ms"), values("upstream_ms")
        decision_counts = Counter((item["safety"] or "Unclassified") for item in snapshots)
        error_counts = Counter(item["error_code"] for item in failed if item["error_code"])
        now_epoch = time.time()
        with self._lock:
            rpm = sum(now_epoch - 60 <= item.received_epoch <= now_epoch
                      for item in list(self._items)[-self.capacity:])
        slowest = sorted(completed, key=lambda item: float(item["total_ms"]), reverse=True)[:5]
        return {
            "generated_at": _iso_now()[0], "capacity": self.capacity, "persistence": self.persistence,
            "window_size": len(snapshots), "completed": len(completed), "success": len(successful),
            "failed": len(failed), "in_flight": sum(item["status"] == "processing" for item in snapshots),
            "success_rate": round(len(successful) * 100 / len(completed), 2) if completed else 0.0,
            "rpm_1m": rpm,
            "latency": {"average_ms": _average(total_values), "p50_ms": _percentile(total_values, .5),
                        "p95_ms": _percentile(total_values, .95), "maximum_ms": max(total_values, default=0.0),
                        "upstream_p95_ms": _percentile(upstream_values, .95)},
            "phases": {"preprocess_average_ms": _average(values("preprocess_ms")),
                       "upstream_average_ms": _average(upstream_values), "response_average_ms": _average(values("response_ms"))},
            "decisions": {key: decision_counts.get(key, 0) for key in ("Safe", "Controversial", "Unsafe", "Unclassified")},
            "errors": [{"code": code, "count": count} for code, count in error_counts.most_common(8)],
            "series": [{"id": item["id"], "label": item["received_at"][11:19], "total_ms": item["total_ms"] or 0,
                        "upstream_ms": item["upstream_ms"] or 0, "status": item["status"], "safety": item["safety"]}
                       for item in reversed(completed[:30])],
            "slowest": [{key: item[key] for key in ("id", "received_at", "total_ms", "upstream_ms", "status", "error_code")}
                        for item in slowest],
            "window": {"oldest_at": snapshots[-1]["received_at"] if snapshots else None,
                       "newest_at": snapshots[0]["received_at"] if snapshots else None},
        }
