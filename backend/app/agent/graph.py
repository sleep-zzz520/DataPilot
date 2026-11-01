"""显式 Agent 图编排（LangGraph StateGraph，替代 create_react_agent 黑盒）。

图结构（面试可画图讲解）：
    START → [agent 节点] --有 tool_calls--> [tools 节点] --→ 回 agent
                         --无 tool_calls--> END
    工具和模型调用先预留本轮共享预算，超过上限直接终止；
    达到查询次数上限时，同时注入反思提示，给模型一次整理现有结果的机会。

对比 create_react_agent（v2）：
- 节点/边/状态全部显式可观测，可打日志、可注入自定义状态（sql_attempts）
- agent 节点用 llm.stream 逐 token 产出（SSE 打字机真实生效；v2 用 invoke 只出完整消息）
"""
from __future__ import annotations

import json
import time
from threading import Event
from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.agent.state import ExecutionBudget

# 同一轮实际 SQL/查询调用硬上限，成功和失败都计入。
MAX_SQL_ATTEMPTS = 3
# 判定为"查询类工具"的名称（用于反思计数）
_QUERY_TOOL_NAMES = ("query_mysql", "query_file")


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    sql_attempts: int  # 本轮 SQL/查询工具调用次数（反思用，防死循环）
    terminal_reason: str
    repair_request: dict


def _filter_new_messages(input_messages: list, result_messages: list) -> list:
    """从 graph 结果中提取本轮新生成的消息（保留给 chat_api 使用）。"""
    return result_messages[len(input_messages):]


def make_graph(llm, tools, max_sql_attempts: int = MAX_SQL_ATTEMPTS,
               trace: Optional["TraceCollector"] = None, agent_name: str = "agent",
               plan_runtime=None, cancel_event: Optional[Event] = None,
               deadline: Optional[float] = None, execution_budget=None,
               max_model_calls: int = 20, max_tool_calls: int = 30,
               max_steps: int = 60, enforce_validation: bool = False,
               max_context_tokens: int = 16000):
    """构造显式编排的 Agent 图。签名与 create_react_agent 用法兼容（chat_api 无需改动）。

    trace：可选轨迹采集器（多智能体模式下主管/专家共享同一个，
           tools 节点自动埋点，得到全链路工具调用链）。
    agent_name：本图所属 Agent 名称，用于轨迹标注（默认单 Agent 场景为 "agent"）。
    """
    tool_map = {t.name: t for t in tools}
    bound_model = llm.bind_tools(tools)
    budget = execution_budget or ExecutionBudget(
        max_model_calls=max_model_calls, max_tool_calls=max_tool_calls,
        max_query_calls=max_sql_attempts, max_steps=max_steps,
    )

    def stop(reason: str) -> dict:
        if plan_runtime is not None:
            plan_runtime.record_worker_failure(agent_name, "execution_stopped", reason)
            plan_runtime.validation_status = "rejected"
            plan_runtime.validation_report = {
                "status": "rejected", "issues": list(plan_runtime.plan.quality_issues),
                "execution_budget": budget.snapshot(), "terminal_reason": reason,
            }
        return {"messages": [AIMessage(content=f"执行已停止：{reason}。请缩小分析范围后重试。")],
                "terminal_reason": reason}

    def requires_evidence() -> bool:
        return plan_runtime is not None and bool(
            plan_runtime.plan.metrics or plan_runtime.plan.evidence
            or any(step.kind == "query" for step in plan_runtime.plan.steps)
        )

    def contracted():
        return plan_runtime is not None and getattr(plan_runtime, "_binding", None) is not None

    def contract_stop(action):
        details = plan_runtime.repair_context()
        if action == "clarify":
            text = "当前指标缺少已确认的口径、必要字段或数据关联证明，请确认定义或补充数据后再分析。"
        else:
            text = "本轮分析未能在修复预算内满足指标契约，已停止，未生成确定性结论。"
        return {"messages": [AIMessage(content=text)], "terminal_reason": "contract_" + action,
                "repair_request": {"action": action, "details": details}}

    def cancelled() -> bool:
        return (cancel_event is not None and cancel_event.is_set()) or (
            deadline is not None and time.monotonic() >= deadline
        )

    # ── 节点 1：agent（LLM 决策，逐 token 流式）─────────────────────────────
    # 注意：必须把 graph 的 config 传给模型调用，langgraph 才能捕获 token
    # 事件（stream_mode="messages" → SSE 逐字打字机）；不传则一次性输出。
    def agent_node(state: AgentState, config) -> dict:
        if cancelled():
            return {"messages": [AIMessage(content="执行已取消：Worker 已超时。")]}
        if contracted() and plan_runtime.next_action() in {"clarify", "stop"}:
            return contract_stop(plan_runtime.next_action())
        from app.memory.context import ContextBudgetExceeded, bound_context_messages

        messages = state["messages"]
        system_parts = [str(m.content) for m in messages if isinstance(m, SystemMessage)]
        if contracted():
            from app.agent.prompts import CONTRACT_HINT
            system_parts.append(CONTRACT_HINT)
        context_messages = [m for m in messages if not isinstance(m, SystemMessage)]
        try:
            bounded = bound_context_messages(
                "\n".join(system_parts), context_messages, tools=tools,
                max_tokens=max_context_tokens,
                output_reserve=int(getattr(llm, "max_tokens", None) or 1024),
            )
        except ContextBudgetExceeded as exc:
            return stop(f"上下文预算不足：{exc}")
        if not budget.reserve("model"):
            return stop(budget.exhausted_reason)
        chunks = list(bound_model.stream(bounded.messages, config))
        if not chunks:
            return {"messages": [AIMessage(content="")]}
        merged = chunks[0]
        for c in chunks[1:]:
            merged = merged + c
        if cancelled():
            return {"messages": [AIMessage(content="执行已取消：Worker 已超时。")]}
        if enforce_validation and not getattr(merged, "tool_calls", None) and requires_evidence():
            report = plan_runtime.validate_evidence(final=True) if contracted() else plan_runtime.validate_evidence()
            if report["status"] != "approved":
                if contracted() and plan_runtime.needs_repair:
                    issue = next((i for i in plan_runtime.plan.active_issues if i.get("step_id")), {})
                    return {"repair_request": {"action": "repair", "step_id": issue.get("step_id")}}
                reasons = "；".join(report.get("issues") or ["证据验证未通过"])
                return {"messages": [AIMessage(content=f"本轮分析未通过证据验证：{reasons}。请补充或修正数据后重试。")],
                        "terminal_reason": "validation_rejected"}
        # AIMessageChunk 是 AIMessage 子类，直接入状态（tool_calls 原样保留）
        return {"messages": [merged]}

    # ── 节点 2：tools（执行工具调用 + 轻量反思）──────────────────────────────
    def tools_node(state: AgentState) -> dict:
        if cancelled():
            return {
                "messages": [AIMessage(content="执行已取消：Worker 已超时。")],
                "sql_attempts": int(state.get("sql_attempts", 0)),
            }
        last = state["messages"][-1]
        tool_calls = getattr(last, "tool_calls", None) or []
        outs: list = []
        attempts = int(state.get("sql_attempts", 0))
        terminal_reason = ""
        repair_request = {}
        for tc in tool_calls:
            if cancelled():
                break
            tool_name = tc.get("name") or "?"
            # Supervisor 的角色入口内部还有一层子图；计划步骤只应记录子图里的真实工具。
            is_worker_entry = agent_name == "supervisor" and tool_name in {
                "data_executor", "statistical_validator", "insight_writer"
            }
            fn = tool_map.get(tool_name)
            entry = trace.begin(agent_name, tool_name, tc.get("args") or {}) if trace else None
            args = dict(tc.get("args") or {})
            if contracted() and not is_worker_entry:
                decision = plan_runtime.preflight_tool(tool_name, args)
                if (tool_name in getattr(plan_runtime, "CALCULATE_TOOLS", set())
                        | getattr(plan_runtime, "VISUALIZE_TOOLS", set())
                        and tool_name not in {"calculate_from_evidence", "chart_from_evidence"}):
                    decision = {"allowed": False, "action": "repair", "step_id": decision.get("step_id"),
                                "issues": [{"code": "RAW_RECORDS_FORBIDDEN",
                                            "expected": "calculate_from_evidence/chart_from_evidence"}]}
                if not decision["allowed"]:
                    outs.append(ToolMessage(content="指标契约校验未通过：" + json.dumps(decision, ensure_ascii=False),
                                            tool_call_id=tc.get("id", ""), name=tool_name))
                    if entry is not None:
                        trace.end(entry, decision, status="error")
                    repair_request = {"action": decision["action"], "step_id": decision.get("step_id"),
                                      "args": args, "issues": decision.get("issues", [])}
                    continue
            if fn is not None and not budget.reserve(
                "tool", query=tool_name in _QUERY_TOOL_NAMES
            ):
                terminal_reason = budget.exhausted_reason
                outs.append(ToolMessage(content=f"执行已阻断：{terminal_reason}",
                                        tool_call_id=tc.get("id", ""), name=tool_name))
                if entry is not None:
                    trace.end(entry, terminal_reason, status="error")
                continue
            if (enforce_validation and plan_runtime is not None
                    and tool_name in {"make_chart", "generate_chart", "auto_analyze_and_visualize"}
                    and plan_runtime.validate_evidence()["status"] != "approved"):
                outs.append(ToolMessage(content="图表已阻断：真实查询证据尚未通过验证。",
                                        tool_call_id=tc.get("id", ""), name=tool_name))
                if entry is not None:
                    trace.end(entry, "图表已阻断：证据未通过验证", status="error")
                continue
            if fn is not None and tool_name in _QUERY_TOOL_NAMES:
                attempts += 1
            if plan_runtime is not None and not is_worker_entry:
                plan_runtime.before_tool(tool_name, tc.get("args") or {})
            if fn is None:
                outs.append(ToolMessage(
                    content=f"错误：未知工具「{tool_name}」，请使用可用工具。",
                    tool_call_id=tc.get("id", ""), name=tool_name))
                if entry is not None:
                    trace.end(entry, "错误：未知工具", status="error")
                continue
            try:
                result = fn.invoke({key: value for key, value in args.items()
                                    if key not in {"plan_step_id", "step_id"}})
                result_text = str(result)
                issues = (
                    plan_runtime.after_tool(tool_name, tc.get("args") or {}, result_text)
                    if plan_runtime is not None and not is_worker_entry else []
                )
                if issues:
                    result_text += "\n【结果质量校验】" + "；".join(issues) + "。请勿生成确定性结论。"
                if contracted() and not is_worker_entry:
                    active = [e for e in plan_runtime.plan.evidence if e.get("active")]
                    if active:
                        result_text += "\n【受控证据引用】" + json.dumps([
                            {k: e.get(k) for k in ("id", "step_id", "version", "columns", "row_count")}
                            for e in active], ensure_ascii=False)
                    if plan_runtime.plan.active_issues:
                        issue = next((i for i in plan_runtime.plan.active_issues if i.get("step_id")), {})
                        repair_request = {"action": plan_runtime.next_action(), "step_id": issue.get("step_id"),
                                          "args": args}
                outs.append(ToolMessage(content=result_text, tool_call_id=tc.get("id", ""), name=tool_name))
                if entry is not None:
                    status = "timeout" if result_text.startswith((
                        "[WORKER_TIMEOUT]", "[TOOL_TIMEOUT]"
                    )) else "busy" if result_text.startswith("[WORKER_BUSY]") else "ok"
                    trace.end(entry, result, status=status)
            except Exception as e:  # noqa: BLE001 —— 工具异常统一转 ToolMessage，交给 LLM 反思
                outs.append(ToolMessage(
                    content=f"工具执行异常：{e}", tool_call_id=tc.get("id", ""), name=tool_name))
                if plan_runtime is not None and is_worker_entry:
                    plan_runtime.record_worker_failure(tool_name, "error", str(e))
                elif plan_runtime is not None:
                    plan_runtime.after_tool(tool_name, tc.get("args") or {}, f"工具执行异常：{e}")
                if entry is not None:
                    trace.end(entry, f"工具执行异常：{e}", status="error")

        # 轻量反思：查询类工具失败/被调用次数达上限 → 强制停止查询，基于已有信息回答
        update: dict = {"messages": outs}
        if repair_request:
            update["repair_request"] = repair_request
        if terminal_reason:
            update.update({"messages": outs + stop(terminal_reason)["messages"],
                           "terminal_reason": terminal_reason, "sql_attempts": attempts})
            return update
        if attempts >= max_sql_attempts:
            update["sql_attempts"] = attempts
            update["messages"] = outs + [SystemMessage(
                content=f"注意：查询工具已连续尝试 {max_sql_attempts} 次。请停止调用任何查询工具，"
                        f"直接根据已掌握的信息回答用户，并如实说明查询遇到的问题。")]
        else:
            update["sql_attempts"] = attempts
        return update

    def repair_node(state: AgentState) -> dict:
        request = state.get("repair_request") or {}
        action = request.get("action", "stop")
        step_id = request.get("step_id")
        if action != "repair" or not step_id:
            return contract_stop(action if action in {"clarify", "stop"} else "clarify")
        entry = trace.begin(agent_name, "local_repair", request) if trace else None
        if not plan_runtime.request_repair(step_id, request.get("args"), "contract_validation"):
            if entry is not None:
                trace.end(entry, "修复上限或不可修复错误", status="error")
            return contract_stop(plan_runtime.next_action())
        if entry is not None:
            trace.end(entry, plan_runtime.plan.repair_history[-1], status="ok")
        return {"messages": [SystemMessage(content=plan_runtime.repair_context()
                    + "\n仅修复指定步骤，保留独立分支有效结果。禁止修改可信指标公式；请按expected重写工具参数。")],
                "repair_request": {}}

    # ── 条件边：agent 有 tool_calls → 走 tools；否则 → END ───────────────────
    def should_continue(state: AgentState):
        if state.get("terminal_reason"):
            return END
        if state.get("repair_request"):
            return "repair"
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("repair", repair_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "repair": "repair", END: END})
    graph.add_conditional_edges("tools", lambda state: END if state.get("terminal_reason") else
                                "repair" if state.get("repair_request") else "agent",
                                {END: END, "agent": "agent", "repair": "repair"})
    graph.add_conditional_edges("repair", lambda state: END if state.get("terminal_reason") else "agent",
                                {END: END, "agent": "agent"})
    compiled = graph.compile().with_config(recursion_limit=max_steps * 2 + 5)
    compiled.execution_budget = budget
    return compiled
