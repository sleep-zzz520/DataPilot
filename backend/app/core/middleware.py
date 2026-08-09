"""可观测性中间件：记录每个 HTTP 请求的 method/path/status/耗时，并写入指标。

用纯 ASGI 形式（而非 BaseHTTPMiddleware），避免对流式响应（SSE）的缓冲破坏；
通过包装 send 捕获响应状态码。
"""
from __future__ import annotations

import time
from typing import Optional

from app.core.metrics import metrics
from app.core.logging import get_logger

logger = get_logger("http")

# 健康检查/指标自身的请求也记录，但噪音低，保留即可


class ObservabilityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        start = time.perf_counter()
        status: Optional[int] = None

        async def wrapped_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        except Exception:
            status = status or 500  # 未捕获异常由 ServerErrorMiddleware 兜底为 500
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            final_status = status or 500
            metrics.record(final_status, duration_ms)
            logger.info("http_request", extra={
                "method": scope.get("method"),
                "path": scope.get("path"),
                "status": final_status,
                "duration_ms": round(duration_ms, 2),
            })
