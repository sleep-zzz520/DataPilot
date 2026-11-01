"""P1 分析角色编排：Supervisor + 执行、验证、表达三个角色。

分析规划器已经由 ``analysis_plan`` StateGraph 在进入本图前执行。本模块只负责：

    Supervisor → data_executor → statistical_validator → insight_writer

Supervisor 只能调用角色入口；验证门禁由 PlanRuntime 确定性执行，避免模型绕过验证
直接给出数值结论。数据库和文件工具合并到 data_executor，图表工具仅在 insight_writer
通过验证后可用。
"""
from __future__ import annotations

import json
import re
import threading
import time
from threading import Event, Thread
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from app.agent.analysis_plan import PlanRuntime, plan_context
from app.agent.graph import make_graph
from app.agent.state import ExecutionBudget
from app.agent.prompts import (
    DATA_EXECUTOR_PROMPT,
    INSIGHT_WRITER_PROMPT,
    STATISTICAL_VALIDATOR_PROMPT,
    SUPERVISOR_PROMPT,
    SINGLE_AGENT_PROMPT,
)
from app.core.timeouts import MAX_CONCURRENT_WORKERS

# 保留工具分组常量，供工具注册和旧调用方复用；角色层不再按 SQL/文件拆专家。
SQL_TOOL_NAMES = ("list_schemas", "get_schema", "get_table_schema", "query_mysql")
FILE_TOOL_NAMES = ("list_files", "query_file", "file_stats")
VIZ_TOOL_NAMES = ("make_chart", "generate_chart", "auto_analyze_and_visualize", "chart_from_evidence")
STAT_TOOL_NAMES = (
    "compare_periods_tool", "compare_groups_tool", "analyze_trend_tool",
    "detect_outliers_tool", "analyze_correlation_tool",
    "calculate_from_evidence",
)
DATA_TOOL_NAMES = SQL_TOOL_NAMES + FILE_TOOL_NAMES

_MARK_RE = re.compile(r"<!--(?:CHART|TABLE|IMAGE_BASE64):.*?-->", re.S)

# Worker 默认最大等待时间。生产环境可由 make_agent(..., worker_timeout_seconds=...) 覆盖。
# 实测（20 用例对照实验）：单个角色在真实网络下常需 30-60 秒，30 秒预算会卡在临界点上，
# 造成约 30% 的用例被超时降级。提到 90 秒以消除这类偶发降级，代价是真超时时等更久。
DEFAULT_WORKER_TIMEOUT_SECONDS = 90.0
_worker_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_WORKERS)


class WorkerTimeoutError(TimeoutError):
    """Worker 在本轮预算内没有返回。"""


def _invoke_with_timeout(fn, timeout_seconds: Optional[float], on_complete=None):
    """在保留同步图 API 的前提下，限制 Worker 的等待时间。

    线程无法安全强杀，所以超时后通过 Event 通知子图停止继续启动新的 LLM/工具调用；
    真正硬取消仍需要异步客户端或进程隔离。daemon 线程避免一个卡死的底层同步调用阻塞进程退出。
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        try:
            return fn()
        finally:
            if on_complete:
                on_complete()

    done = Event()
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样交给调用方处理
            outcome["error"] = exc
        finally:
            done.set()
            if on_complete:
                on_complete()

    thread = Thread(target=run, name="agent-worker", daemon=True)
    thread.start()
    if not done.wait(timeout_seconds):
        raise WorkerTimeoutError(f"Worker 执行超过 {timeout_seconds:g} 秒")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def _timeout_message(worker: str, timeout_seconds: float) -> str:
    return (
        f"[WORKER_TIMEOUT] {worker} 在 {timeout_seconds:g} 秒内未完成。"
        "本轮不应基于未完成的证据生成确定性结论，请缩小查询范围后重试。"
    )


def _busy_message(worker: str) -> str:
    return (
        f"[WORKER_BUSY] {worker} 当前资源繁忙，未启动执行。"
        "请稍后重试，或缩小分析范围。"
    )


def _worker_expired(deadline: Optional[float]) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _make_role_tool(
    name: str,
    description: str,
    llm,
    tools: list,
    system_prompt: str,
    trace=None,
    plan_runtime: Optional[PlanRuntime] = None,
    before=None,
    after=None,
    worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
    worker_semaphore=None,
    execution_budget=None,
    max_context_tokens: int = 16000,
):
    """构造角色入口；角色内部仍是独立显式子图，保留调用链和职责边界。"""

    @tool
    def role(request: str) -> str:
        """角色入口。"""
        if before:
            blocked = before(request)
            if blocked:
                return blocked
        semaphore = worker_semaphore or _worker_semaphore
        if not semaphore.acquire(blocking=False):
            if plan_runtime is not None:
                plan_runtime.record_worker_failure(name, "busy")
            return _busy_message(name)
        if plan_runtime is not None and plan_runtime.has_contracts:
            system_prompt_for_run = system_prompt + "\n" + plan_context(plan_runtime.plan)
        else:
            system_prompt_for_run = system_prompt
        msgs = [SystemMessage(content=system_prompt_for_run), HumanMessage(content=request)]
        cancel_event = threading.Event()
        deadline = (
            time.monotonic() + worker_timeout_seconds
            if worker_timeout_seconds and worker_timeout_seconds > 0 else None
        )

        def invoke_worker():
            # 每次调用创建带独立取消信号的子图，避免一次超时污染后续请求。
            subgraph = make_graph(
                llm,
                tools,
                trace=trace,
                agent_name=name,
                plan_runtime=plan_runtime,
                cancel_event=cancel_event,
                deadline=deadline,
                execution_budget=execution_budget,
                enforce_validation=name == "insight_writer",
                max_context_tokens=max_context_tokens,
            )
            return subgraph.invoke({"messages": msgs})

        try:
            result = _invoke_with_timeout(
                invoke_worker,
                max(0.0, deadline - time.monotonic()) if deadline is not None else None,
                on_complete=semaphore.release,
            )
        except WorkerTimeoutError:
            cancel_event.set()
            if plan_runtime is not None:
                plan_runtime.record_worker_failure(
                    name, "timeout", f"超过 {worker_timeout_seconds:g} 秒"
            )
            return _timeout_message(name, worker_timeout_seconds)
        if _worker_expired(deadline):
            cancel_event.set()
            if plan_runtime is not None:
                plan_runtime.record_worker_failure(
                    name, "timeout", f"超过 {worker_timeout_seconds:g} 秒"
                )
            return _timeout_message(name, worker_timeout_seconds)
        sub_msgs = result["messages"]
        last = sub_msgs[-1]
        reply = str(getattr(last, "content", None) or "")
        # 子图内的图表/表格标记必须回传给 Supervisor，供 API 提取和前端渲染。
        marks: list[str] = []
        for message in sub_msgs:
            content = str(getattr(message, "content", None) or "")
            for mark in _MARK_RE.findall(content):
                if mark not in marks:
                    marks.append(mark)
        if marks:
            reply = (reply.rstrip() + "\n" + "\n".join(marks)).strip()
        if after:
            reply = after(reply)
        return reply

    role.name = name
    role.description = description
    return role


def _make_validator_tool(plan_runtime: Optional[PlanRuntime], llm=None,
                         statistical_tools: Optional[list] = None, trace=None,
                         worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
                         worker_semaphore=None, execution_budget=None,
                         max_context_tokens: int = 16000):
    """创建统计验证角色：先做确定性门禁，再用 Python 工具完成可复核计算。"""

    # 直接调用 make_agent 的旧兼容场景没有运行时证据，保留原来的零额外调用行为；
    # 生产 API 始终传入 PlanRuntime，统计子图在真实证据通过门禁后启用。
    has_subgraph = plan_runtime is not None and llm is not None and statistical_tools

    @tool
    def statistical_validator(request: str) -> str:
        """检查本轮真实证据、样本量、口径、计算和异常结果。"""
        if plan_runtime is None:
            report: dict[str, Any] = {
                "status": "approved",
                "evidence_count": 0,
                "evidence": [],
                "issues": [],
                "note": "未提供 PlanRuntime，兼容直接调用场景；生产请求始终启用确定性验证。",
            }
        else:
            report = plan_runtime.validate_evidence()
        calculation = ""
        if report["status"] == "approved" and has_subgraph:
            semaphore = worker_semaphore or _worker_semaphore
            if not semaphore.acquire(blocking=False):
                if plan_runtime is not None:
                    plan_runtime.record_worker_failure("statistical_validator", "busy")
                return _busy_message("statistical_validator")
            cancel_event = threading.Event()
            deadline = (
                time.monotonic() + worker_timeout_seconds
                if worker_timeout_seconds and worker_timeout_seconds > 0 else None
            )

            def invoke_validator():
                subgraph = make_graph(
                    llm,
                    statistical_tools,
                    trace=trace,
                    agent_name="statistical_validator",
                    plan_runtime=plan_runtime,
                    cancel_event=cancel_event,
                    deadline=deadline,
                    execution_budget=execution_budget,
                    max_context_tokens=max_context_tokens,
                )
                return subgraph.invoke({
                    "messages": [
                        SystemMessage(content=STATISTICAL_VALIDATOR_PROMPT + (
                            "\n" + plan_context(plan_runtime.plan) if plan_runtime.has_contracts else "")),
                        HumanMessage(content=request),
                    ]
                })

            try:
                result = _invoke_with_timeout(
                    invoke_validator,
                    max(0.0, deadline - time.monotonic()) if deadline is not None else None,
                    on_complete=semaphore.release,
                )
            except WorkerTimeoutError:
                cancel_event.set()
                if plan_runtime is not None:
                    plan_runtime.record_worker_failure(
                        "statistical_validator",
                        "timeout",
                        f"超过 {worker_timeout_seconds:g} 秒",
                )
                return _timeout_message("statistical_validator", worker_timeout_seconds)
            if _worker_expired(deadline):
                cancel_event.set()
                if plan_runtime is not None:
                    plan_runtime.record_worker_failure(
                        "statistical_validator",
                        "timeout",
                        f"超过 {worker_timeout_seconds:g} 秒",
                    )
                return _timeout_message("statistical_validator", worker_timeout_seconds)
            messages = result.get("messages") or []
            if result.get("terminal_reason"):
                report = dict(plan_runtime.validation_report)
                report["status"] = "rejected"
                report.setdefault("issues", [result["terminal_reason"]])
            if messages:
                # 不只保留 LLM 的摘要，必须把统计工具的结构化 JSON 一并回传，
                # 否则 Supervisor 看不到 parameters/sample_size/intermediate/result。
                tool_outputs = [
                    str(getattr(message, "content", None) or "")
                    for message in messages
                    if getattr(message, "type", "") == "tool"
                ]
                final_note = str(getattr(messages[-1], "content", None) or "")
                calculation = "\n".join(tool_outputs)
                if final_note and final_note not in calculation:
                    calculation = (calculation + "\n统计验证说明：" + final_note).strip()
                calculation_errors = []
                for output in tool_outputs:
                    try:
                        payload = json.loads(output)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(payload, dict) and payload.get("status") == "error":
                        calculation_errors.append(payload.get("message") or "统计工具返回错误")
                if calculation_errors:
                    issue = "统计工具计算失败：" + "；".join(calculation_errors)
                    if issue not in report["issues"]:
                        report["issues"].append(issue)
                    report["status"] = "rejected"
                    if plan_runtime is not None:
                        if issue not in plan_runtime.plan.quality_issues:
                            plan_runtime.plan.quality_issues.append(issue)
                        plan_runtime.validation_status = "rejected"
                        plan_runtime.validation_report = dict(report)
        status = "通过" if report["status"] == "approved" else "不通过"
        issues = "；".join(report.get("issues") or []) or "未发现已知质量问题"
        return (
            f"统计验证：{status}\n"
            f"证据数：{report.get('evidence_count', 0)}\n"
            f"检查结果：{issues}\n"
            f"验证报告：{report}"
            + (f"\n可复核统计计算：{calculation}" if calculation else "")
        )

    statistical_validator.name = "statistical_validator"
    return statistical_validator


def decide_route(question: str = "", plan_runtime=None, mode: str = "auto") -> dict:
    """Explain the deterministic routing choice without another model call."""
    if mode not in {"auto", "single", "multi"}:
        raise ValueError("mode must be auto, single or multi")
    reasons = []
    if mode != "auto":
        selected = mode
        reasons.append("explicit_override")
    elif not question.strip():
        selected = "multi"
        reasons.append("missing_question_compatibility")
    else:
        complex_terms = ("同比", "环比", "趋势", "异常", "相关", "归因", "预测", "显著", "跨库", "联合", "综合", "报告")
        intents = [word for word in complex_terms if word in question]
        plan = plan_runtime.plan if plan_runtime is not None else None
        if plan is not None:
            if len(plan.data_sources) > 1:
                reasons.append("multiple_sources")
            # Multiple SQL queries on one source do not establish a benefit from
            # extra model roles. Require actual planned statistical work as well.
            if any(step.kind == "calculate" for step in plan.steps) and intents:
                reasons.append("planned_statistical_validation")
        elif intents:
            reasons.append("complex_intent_without_plan")
        selected = "multi" if reasons else "single"
        if not reasons:
            reasons.append("bounded_simple_analysis")
    return {"requested_mode": mode, "selected_mode": selected, "reasons": reasons}


def make_agent(
    llm,
    all_tools: list,
    trace=None,
    plan_runtime: Optional[PlanRuntime] = None,
    worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
    worker_semaphore=None,
    mode: str = "auto",
    question: str = "",
    max_model_calls: int = 20,
    max_tool_calls: int = 30,
    max_query_calls: int = 3,
    max_steps: int = 60,
    max_context_tokens: int = 16000,
):
    """构建 P1 分析角色图，返回 ``(graph, supervisor_prompt)``。

    生产请求始终传入 PlanRuntime，因此 insight_writer 在验证未通过时会被硬阻断。
    ``all_tools`` 中未被识别的工具仍使用旧的单 Agent 回退，避免影响非分析工具的直接测试。
    """
    if plan_runtime is not None and plan_runtime.has_contracts:
        from app.tools.evidence_tool import make_evidence_tools
        all_tools = list(all_tools) + make_evidence_tools(plan_runtime, all_tools)
    decision = decide_route(question, plan_runtime, mode)
    budget = ExecutionBudget(max_model_calls=max_model_calls, max_tool_calls=max_tool_calls,
                             max_query_calls=max_query_calls, max_steps=max_steps)

    def finish(graph, prompt):
        graph.route_decision = decision
        return graph, prompt

    if decision["selected_mode"] == "single":
        return finish(make_graph(
            llm, all_tools, trace=trace, agent_name="agent", plan_runtime=plan_runtime,
            execution_budget=budget, enforce_validation=True,
            max_context_tokens=max_context_tokens,
        ), SINGLE_AGENT_PROMPT)

    by_name = {t.name: t for t in all_tools}
    data_tools = [by_name[n] for n in DATA_TOOL_NAMES if n in by_name]
    viz_tools = [by_name[n] for n in VIZ_TOOL_NAMES if n in by_name]
    statistical_tools = [by_name[n] for n in STAT_TOOL_NAMES if n in by_name]

    role_tools: list = []
    if data_tools:
        role_tools.append(_make_role_tool(
            "data_executor",
            "负责 Schema、MySQL SELECT 和上传文件的真实查询，只返回可复核数据证据。",
            llm, data_tools, DATA_EXECUTOR_PROMPT, trace, plan_runtime,
            worker_timeout_seconds=worker_timeout_seconds,
            worker_semaphore=worker_semaphore,
            execution_budget=budget,
            max_context_tokens=max_context_tokens,
        ))
    if data_tools or plan_runtime is not None:
        role_tools.append(_make_validator_tool(
            plan_runtime,
            llm=llm,
            statistical_tools=statistical_tools,
            trace=trace,
            worker_timeout_seconds=worker_timeout_seconds,
            worker_semaphore=worker_semaphore,
            execution_budget=budget,
            max_context_tokens=max_context_tokens,
        ))

    def require_validation(_request: str) -> Optional[str]:
        if plan_runtime is not None and plan_runtime.validation_status != "approved":
            return (
                "洞察表达已停止：统计验证尚未通过。请先调用 statistical_validator，"
                "并仅基于通过验证的证据继续。"
            )
        return None

    if viz_tools or plan_runtime is not None:
        role_tools.append(_make_role_tool(
            "insight_writer",
            "基于 statistical_validator 通过的证据生成结论、限制、建议，并按需生成图表。",
            llm, viz_tools, INSIGHT_WRITER_PROMPT, trace, plan_runtime,
            before=require_validation,
            worker_timeout_seconds=worker_timeout_seconds,
            worker_semaphore=worker_semaphore,
            execution_budget=budget,
            max_context_tokens=max_context_tokens,
        ))

    if not role_tools:
        # 非分析工具（如单独的测试工具）保留兼容回退；真实分析请求不会走这里。
        return finish(make_graph(
            llm, all_tools, trace=trace, agent_name="agent", plan_runtime=plan_runtime,
            execution_budget=budget, enforce_validation=True,
            max_context_tokens=max_context_tokens,
        ), SUPERVISOR_PROMPT)

    return finish(make_graph(
        llm, role_tools, trace=trace, agent_name="supervisor", plan_runtime=plan_runtime,
        execution_budget=budget, enforce_validation=True,
        max_context_tokens=max_context_tokens,
    ), SUPERVISOR_PROMPT)
