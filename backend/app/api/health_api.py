"""运维端点：健康检查（liveness / readiness）+ 请求指标。

- GET /api/health          liveness：进程存活即 200（K8s/Docker 存活探针）
- GET /api/health/ready    readiness：检查 SQLite 可连接、data 目录可写；不通过返回 503
- GET /api/metrics         请求数 / 状态码分布 / 错误率 / 耗时分位数（见 app.core.metrics）
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import persistence
from app.core.metrics import metrics
from app.core.logging import get_logger
from app.db.query_cache import get_query_cache
from app.db.query_runtime import query_runtime

logger = get_logger("health")

router = APIRouter(tags=["ops"])

_SERVICE = "data-analysis-agent"


@router.get("/api/health")
def health():
    return {"ok": True, "service": _SERVICE}


@router.get("/api/health/ready")
def ready():
    checks: dict = {}
    ok = True

    # 1) SQLite 可连接
    try:
        conn = persistence._conn()
        try:
            conn.execute("SELECT 1").fetchone()
            checks["sqlite"] = "ok"
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 —— 健康检查就是要兜住一切异常
        ok = False
        checks["sqlite"] = f"error: {e}"

    # 2) data 目录可写（上传/会话落盘的前提）
    try:
        probe = persistence._DB_DIR / ".health_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks["data_dir"] = "ok"
    except Exception as e:  # noqa: BLE001
        ok = False
        checks["data_dir"] = f"error: {e}"

    if not ok:
        logger.error("readiness_failed", extra={"checks": checks})
        return JSONResponse({"ok": False, "service": _SERVICE, "checks": checks}, status_code=503)
    return {"ok": True, "service": _SERVICE, "checks": checks}


@router.get("/api/metrics")
def api_metrics():
    snapshot = metrics.snapshot()
    snapshot["query_cache"] = get_query_cache().stats()
    snapshot["query_runtime"] = query_runtime.snapshot()
    return snapshot
