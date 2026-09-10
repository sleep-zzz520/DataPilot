"""本地工具进程隔离：结果传回、子进程异常与超时回收。"""
import operator
import time

import pytest

from app.core.process_executor import (
    LocalToolProcessError,
    LocalToolTimeoutError,
    run_in_process,
)


def test_process_executor_returns_picklable_result():
    assert run_in_process(operator.add, 2, 3, timeout_seconds=2) == 5


def test_process_executor_returns_child_error():
    with pytest.raises(LocalToolProcessError, match="ValueError"):
        run_in_process(int, "not-a-number", timeout_seconds=2)


def test_process_executor_terminates_timeout():
    started = time.monotonic()
    with pytest.raises(LocalToolTimeoutError):
        run_in_process(
            time.sleep,
            2,
            timeout_seconds=0.05,
            terminate_grace_seconds=0.05,
        )
    assert time.monotonic() - started < 1
