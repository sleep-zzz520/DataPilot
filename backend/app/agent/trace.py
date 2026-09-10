"""Agent 轨迹采集（工具调用链可视化）。

设计（面试可讲）：
    显式图编排让工具执行点收敛在 tools 节点，轨迹采集天然是"节点埋点"，
    不需要侵入任何工具实现。主管图与专家子图共享同一个 TraceCollector，
    通过 begin/end 的栈深度得到调用层级，一轮对话即可还原完整链路：

        seq=1  supervisor   data_executor     (depth 0)
        seq=2  data_executor query_mysql      (depth 1)
        seq=3  data_executor get_schema       (depth 1)
        seq=4  supervisor   insight_writer    (depth 0)
        ...

每条 entry：
    seq          全局序号（前端时间线排序）
    agent        发起调用的 Agent（supervisor / data_executor / statistical_validator /
                 insight_writer / agent）
    tool         工具名
    depth        调用层级（0 = 主管直接调用；专家内部工具 = 1）
    input        入参摘要（截断为单行）
    output       结果摘要（截断为单行）
    status       running / ok / error / timeout / busy
    duration_ms  工具执行耗时
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

# 轨迹摘要上限：够演示、够排查，又不把大数据量结果塞进前端/DB
TRACE_MAX_CHARS = 300


def summarize(value: Any, max_chars: int = TRACE_MAX_CHARS) -> str:
    """把任意入参/结果转成单行截断摘要。"""
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 —— 不可序列化对象兜底
            s = str(value)
    s = " ".join(str(s).split()).strip()  # 压缩换行/空白为单行
    if len(s) > max_chars:
        return s[:max_chars] + f"…(共 {len(s)} 字符)"
    return s


class TraceCollector:
    """共享轨迹采集器：主管图与专家子图同时向它追加，保证全链路连续。"""

    def __init__(self) -> None:
        self.entries: list = []
        self._seq: int = 0
        self._stack: list = []  # 进行中的父级调用（决定 depth）

    def begin(self, agent: str, tool: str, args: Any) -> dict:
        """记录一次工具调用的开始，返回 entry 供 end() 收尾。"""
        self._seq += 1
        entry = {
            "seq": self._seq,
            "agent": agent,
            "tool": tool,
            "depth": len(self._stack),
            "input": summarize(args),
            "output": None,
            "status": "running",
            "duration_ms": None,
        }
        self._stack.append(agent)
        self.entries.append(entry)
        entry["_started_at"] = time.time()  # 内部字段：end() 时换算耗时，不对外输出
        return entry

    def end(self, entry: dict, output: Any, status: str = "ok") -> None:
        """记录工具调用结果（status: ok / error）。"""
        entry["output"] = summarize(output)
        entry["status"] = status
        started = entry.pop("_started_at", None)
        if started is not None:
            entry["duration_ms"] = round((time.time() - started) * 1000)
        if self._stack:
            self._stack.pop()

    def snapshot(self) -> list:
        """返回当前已记录的完整轨迹（副本，防止调用方意外改动内部状态）。"""
        return list(self.entries)
