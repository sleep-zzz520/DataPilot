"""可观测性（日志 / 指标 / 健康检查）单测 + 端点集成测试。"""
import io
import json
import logging
import time

import pytest

from app.core.logging import setup_logging, get_logger
from app.core.metrics import Metrics
from app.core.middleware import ObservabilityMiddleware


# ── 结构化日志 ────────────────────────────────────────────────────────────────
def test_json_logger_outputs_json_line():
    buf = io.StringIO()
    setup_logging(level="INFO", json_logs=True, stream=buf)
    get_logger("test").info("hello_world", extra={"method": "GET", "status": 200})
    line = buf.getvalue().strip().splitlines()[-1]
    data = json.loads(line)
    assert data["msg"] == "hello_world"
    assert data["level"] == "INFO"
    assert data["logger"] == "test"
    assert data["method"] == "GET" and data["status"] == 200
    assert "ts" in data


def test_json_logger_includes_exception():
    buf = io.StringIO()
    setup_logging(level="INFO", json_logs=True, stream=buf)
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("test").exception("failed")
    data = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert "ValueError" in data["exc"]


def test_plain_logger_human_readable():
    buf = io.StringIO()
    setup_logging(level="INFO", json_logs=False, stream=buf)
    get_logger("test").info("plain text")
    assert buf.getvalue().strip() == "[INFO] test: plain text"


def test_setup_logging_idempotent():
    buf = io.StringIO()
    setup_logging(json_logs=True, stream=buf)
    setup_logging(json_logs=True, stream=buf)  # 再调一次不应报错
    get_logger("test").info("x")
    assert buf.getvalue().count("x") == 1  # 旧 handler 已被清掉，不会重复输出


# ── 指标聚合 ──────────────────────────────────────────────────────────────────
def test_metrics_records_requests_and_status():
    m = Metrics()
    m.record(200, 10.0)
    m.record(200, 20.0)
    m.record(500, 500.0)
    snap = m.snapshot()
    assert snap["total_requests"] == 3
    assert snap["status_counts"] == {200: 2, 500: 1}
    assert snap["error_rate_5xx"] == pytest.approx(1 / 3, abs=1e-4)


def test_metrics_latency_percentiles():
    m = Metrics()
    for ms in [10, 20, 30, 40, 50]:
        m.record(200, ms)
    snap = m.snapshot()
    assert snap["latency_ms"]["avg"] == 30.0
    assert snap["latency_ms"]["max"] == 50.0
    # p50/p95 是有序样本的百分位索引取值
    assert snap["latency_ms"]["p50"] == 30.0
    assert snap["latency_ms"]["p95"] == 50.0


def test_metrics_empty_snapshot():
    snap = Metrics().snapshot()
    assert snap["total_requests"] == 0
    assert snap["latency_ms"]["p50"] is None
    assert snap["error_rate_5xx"] == 0.0


def test_metrics_window_expiry():
    m = Metrics(window_seconds=0)  # 窗口 0 秒 → 记录后立即过期
    m.record(500, 1.0)
    snap = m.snapshot()
    assert snap["window"]["requests"] == 0
    assert snap["window"]["error_rate_5xx"] == 0.0
    # 全局指标不受影响
    assert snap["total_requests"] == 1 and snap["error_rate_5xx"] == 1.0


def test_metrics_bounded_latency_samples():
    m = Metrics(max_latency_samples=3)
    for ms in range(10):
        m.record(200, ms)
    snap = m.snapshot()
    assert snap["latency_ms"]["max"] == 9.0  # 环形缓冲保留最近 3 条：7, 8, 9


# ── 中间件 ────────────────────────────────────────────────────────────────────
def test_middleware_records_metrics_and_status():
    import asyncio
    from unittest.mock import patch

    class FakeApp:
        """返回 201 的伪 ASGI 应用。"""
        async def __call__(self, scope, receive, send):
            await send({"type": "http.response.start", "status": 201, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

    m = Metrics()
    calls = []

    def capture(name, **kw):
        calls.append({"name": name, **kw})

    async def run():
        sent = []
        async def fake_send(msg):
            sent.append(msg)
        with patch("app.core.middleware.metrics", m), patch("app.core.middleware.logger") as mock_logger:
            mock_logger.info = capture
            await ObservabilityMiddleware(FakeApp())(
                {"type": "http", "method": "GET", "path": "/api/x"}, None, fake_send)

    asyncio.run(run())
    snap = m.snapshot()
    assert snap["total_requests"] == 1
    assert snap["status_counts"] == {201: 1}
    assert calls[0]["name"] == "http_request"
    assert calls[0]["extra"]["status"] == 201
    assert calls[0]["extra"]["method"] == "GET"
    assert calls[0]["extra"]["path"] == "/api/x"


# ── 健康检查 / 指标端点（集成）───────────────────────────────────────────────
def test_health_liveness(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_ready_ok(client):
    r = client.get("/api/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["checks"]["sqlite"] == "ok"
    assert body["checks"]["data_dir"] == "ok"


def test_health_ready_fails_when_sqlite_broken(client, monkeypatch):
    from pathlib import Path
    import app.persistence as persistence_mod
    monkeypatch.setattr(persistence_mod, "_DB_PATH", Path("/nonexistent-dir/x.db"))
    monkeypatch.setattr(persistence_mod, "_DB_DIR", Path("/nonexistent-dir"))
    r = client.get("/api/health/ready")
    assert r.status_code == 503
    assert r.json()["ok"] is False


def test_metrics_endpoint_counts_requests(client):
    client.get("/api/health")          # 产生 1 个请求
    client.get("/api/health")          # 再 1 个
    r = client.get("/api/metrics")
    body = r.json()
    assert body["total_requests"] >= 3  # 加上 metrics 自身这 1 个
    assert "status_counts" in body and "latency_ms" in body
    assert "query_cache" in body and "query_runtime" in body
    assert body["status_counts"].get("200", 0) >= 2
