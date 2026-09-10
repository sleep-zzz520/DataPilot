"""可终止的本地工具执行器。

线程无法可靠停止 pandas、DuckDB 或 Matplotlib 已经开始的同步调用；本模块只
隔离可序列化的纯本地任务，超时后终止子进程并等待其退出。LLM 和数据库连接不
经过这里，分别由其原生客户端/服务端超时控制。
"""
from __future__ import annotations

import multiprocessing as mp
import time
from multiprocessing.connection import wait
from typing import Any, Callable, Optional

from app.core.timeouts import (
    LOCAL_TOOL_TERMINATE_GRACE_SECONDS,
    LOCAL_TOOL_TIMEOUT_SECONDS,
)


class LocalToolTimeoutError(TimeoutError):
    """本地工具在预算内没有完成，子进程已被回收。"""


class LocalToolProcessError(RuntimeError):
    """子进程未能返回可用结果。"""


def _run_child(send_conn, fn: Callable, args: tuple, kwargs: dict) -> None:
    try:
        send_conn.send(("ok", fn(*args, **kwargs)))
    except BaseException as exc:  # noqa: BLE001 - 必须向父进程传递所有失败
        send_conn.send(("error", type(exc).__name__, str(exc)))
    finally:
        send_conn.close()


def _stop_process(process, grace_seconds: float) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(grace_seconds)
    if process.is_alive():
        process.kill()
        process.join()


def run_in_process(
    fn: Callable,
    *args: Any,
    timeout_seconds: float = LOCAL_TOOL_TIMEOUT_SECONDS,
    terminate_grace_seconds: float = LOCAL_TOOL_TERMINATE_GRACE_SECONDS,
    **kwargs: Any,
) -> Any:
    """在 ``spawn`` 子进程执行纯函数，超时后强制回收子进程。

    ``spawn`` 避免继承 Web 进程中的线程、数据库连接与 matplotlib 状态；调用方
    只能传递可 pickle 的函数、参数和返回值。父进程在子进程发送结果时立即读取，
    避免大 base64 图像塞满 Pipe 后阻塞子进程退出。
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")
    context = mp.get_context("spawn")
    receive_conn, send_conn = context.Pipe(duplex=False)
    process = context.Process(target=_run_child, args=(send_conn, fn, args, kwargs))
    process.start()
    send_conn.close()
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process, terminate_grace_seconds)
                raise LocalToolTimeoutError(f"本地工具执行超过 {timeout_seconds:g} 秒")
            if receive_conn in wait([receive_conn], timeout=remaining):
                try:
                    outcome = receive_conn.recv()
                except EOFError as exc:
                    _stop_process(process, terminate_grace_seconds)
                    raise LocalToolProcessError("本地工具进程异常退出，未返回结果") from exc
                _stop_process(process, terminate_grace_seconds)
                if outcome[0] == "ok":
                    return outcome[1]
                raise LocalToolProcessError(f"{outcome[1]}：{outcome[2]}")
            if not process.is_alive():
                raise LocalToolProcessError("本地工具进程异常退出，未返回结果")
    finally:
        receive_conn.close()
        _stop_process(process, terminate_grace_seconds)
