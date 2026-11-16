"""Bounded in-memory runtime metrics for executed queries.

The middleware metrics cover HTTP requests; this module covers the database and
local-file queries behind a request. Slow executions are additionally written to
the append-only audit log through ``record_query_runtime``.
"""
from __future__ import annotations

import os
import threading
from collections import deque
from typing import Optional

_MAX_SAMPLES = 2000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


SLOW_QUERY_MS = _env_int("SLOW_QUERY_MS", 1000)


class QueryRuntimeMetrics:
    def __init__(self, max_samples: int = _MAX_SAMPLES):
        self._lock = threading.Lock()
        self._samples: deque = deque(maxlen=max_samples)
        self._total = 0
        self._errors = 0
        self._slow = 0
        self._by_source: dict[str, dict] = {}

    def record(self, source: str, duration_ms: float, *,
               rows: int = 0, ok: bool = True) -> bool:
        duration_ms = max(0.0, float(duration_ms))
        slow = duration_ms >= SLOW_QUERY_MS
        with self._lock:
            self._total += 1
            if not ok:
                self._errors += 1
            if slow:
                self._slow += 1
            self._samples.append(duration_ms)
            bucket = self._by_source.setdefault(
                source, {"total": 0, "errors": 0, "slow": 0, "samples": deque(maxlen=500)}
            )
            bucket["total"] += 1
            if not ok:
                bucket["errors"] += 1
            if slow:
                bucket["slow"] += 1
            bucket["samples"].append(duration_ms)
        return slow

    def snapshot(self) -> dict:
        with self._lock:
            samples = sorted(self._samples)
            by_source = {}
            for name, bucket in sorted(self._by_source.items()):
                source_samples = sorted(bucket["samples"])
                by_source[name] = {
                    "total": bucket["total"],
                    "errors": bucket["errors"],
                    "slow": bucket["slow"],
                    "avg_ms": round(sum(source_samples) / len(source_samples), 2) if source_samples else None,
                    "p95_ms": _percentile(source_samples, 0.95),
                    "max_ms": round(source_samples[-1], 2) if source_samples else None,
                }
            return {
                "slow_threshold_ms": SLOW_QUERY_MS,
                "total_queries": self._total,
                "errors": self._errors,
                "slow_queries": self._slow,
                "slow_rate": round(self._slow / self._total, 4) if self._total else 0.0,
                "latency_ms": {
                    "avg": round(sum(samples) / len(samples), 2) if samples else None,
                    "p50": _percentile(samples, 0.50),
                    "p95": _percentile(samples, 0.95),
                    "p99": _percentile(samples, 0.99),
                    "max": round(samples[-1], 2) if samples else None,
                },
                "sources": by_source,
            }

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._total = 0
            self._errors = 0
            self._slow = 0
            self._by_source.clear()


def _percentile(sorted_samples: list, p: float):
    if not sorted_samples:
        return None
    idx = min(len(sorted_samples) - 1, int(p * len(sorted_samples)))
    return round(sorted_samples[idx], 2)


query_runtime = QueryRuntimeMetrics()


def record_query_runtime(source: str, duration_ms: float, *, rows: int = 0,
                         ok: bool = True, audit_ctx: Optional[dict] = None,
                         sql: Optional[str] = None) -> bool:
    """Record one query and write a slow-query audit entry when applicable."""
    slow = query_runtime.record(source, duration_ms, rows=rows, ok=ok)
    if slow and audit_ctx:
        try:
            from app.audit import A_SLOW_QUERY, record as audit_record
            audit_record(A_SLOW_QUERY, audit_ctx.get("user_id"), audit_ctx.get("username"), {
                "sql": (sql or "")[:4000],
                "source": source,
                "rows": rows,
                "ok": ok,
                "duration_ms": round(max(0.0, float(duration_ms)), 2),
                "threshold_ms": SLOW_QUERY_MS,
                "session_id": audit_ctx.get("session_id"),
            })
        except Exception:  # noqa: BLE001 - telemetry must never fail the query
            pass
    return slow
