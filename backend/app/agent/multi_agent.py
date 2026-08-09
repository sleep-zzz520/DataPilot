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
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from app.agent.analysis_plan import PlanRuntime
from app.agent.graph import make_graph
from app.agent.prompts import (
    DATA_EXECUTOR_PROMPT,
    INSIGHT_WRITER_PROMPT,
    STATISTICAL_VALIDATOR_PROMPT,
    SUPERVISOR_PROMPT,
)

# 保留工具分组常量，供工具注册和旧调用方复用；角色层不再按 SQL/文件拆专家。
SQL_TOOL_NAMES = ("list_schemas", "get_schema", "get_table_schema", "query_mysql")
FILE_TOOL_NAMES = ("list_files", "query_file", "file_stats")
VIZ_TOOL_NAMES = ("make_chart", "generate_chart", "auto_analyze_and_visualize")
STAT_TOOL_NAMES = (
    "compare_periods_tool", "compare_groups_tool", "analyze_trend_tool",
    "detect_outliers_tool", "analyze_correlation_tool",
)
DATA_TOOL_NAMES = SQL_TOOL_NAMES + FILE_TOOL_NAMES

_MARK_RE = re.compile(r"<!--(?:CHART|TABLE|IMAGE_BASE64):.*?-->", re.S)


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
):
    """构造角色入口；角色内部仍是独立显式子图，保留调用链和职责边界。"""
    subgraph = make_graph(
        llm, tools, trace=trace, agent_name=name, plan_runtime=plan_runtime
    )

    @tool
    def role(request: str) -> str:
        """角色入口。"""
        if before:
            blocked = before(request)
            if blocked:
                return blocked
        msgs = [SystemMessage(content=system_prompt), HumanMessage(content=request)]
        result = subgraph.invoke({"messages": msgs})
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
                         statistical_tools: Optional[list] = None, trace=None):
    """创建统计验证角色：先做确定性门禁，再用 Python 工具完成可复核计算。"""

    subgraph = None
    # 直接调用 make_agent 的旧兼容场景没有运行时证据，保留原来的零额外调用行为；
    # 生产 API 始终传入 PlanRuntime，统计子图在真实证据通过门禁后启用。
    if plan_runtime is not None and llm is not None and statistical_tools:
        subgraph = make_graph(
            llm, statistical_tools, trace=trace, agent_name="statistical_validator"
        )

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
        if report["status"] == "approved" and subgraph is not None:
            result = subgraph.invoke({
                "messages": [
                    SystemMessage(content=STATISTICAL_VALIDATOR_PROMPT),
                    HumanMessage(content=request),
                ]
            })
            messages = result.get("messages") or []
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
                    if payload.get("status") == "error":
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


def make_agent(llm, all_tools: list, trace=None, plan_runtime: Optional[PlanRuntime] = None):
    """构建 P1 分析角色图，返回 ``(graph, supervisor_prompt)``。

    生产请求始终传入 PlanRuntime，因此 insight_writer 在验证未通过时会被硬阻断。
    ``all_tools`` 中未被识别的工具仍使用旧的单 Agent 回退，避免影响非分析工具的直接测试。
    """
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
        ))
    if data_tools or plan_runtime is not None:
        role_tools.append(_make_validator_tool(
            plan_runtime, llm=llm, statistical_tools=statistical_tools, trace=trace
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
        ))

    if not role_tools:
        # 非分析工具（如单独的测试工具）保留兼容回退；真实分析请求不会走这里。
        return make_graph(
            llm, all_tools, trace=trace, agent_name="agent", plan_runtime=plan_runtime
        ), SUPERVISOR_PROMPT

    return make_graph(
        llm, role_tools, trace=trace, agent_name="supervisor", plan_runtime=plan_runtime
    ), SUPERVISOR_PROMPT
