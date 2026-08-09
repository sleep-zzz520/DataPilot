"""显式 Agent 图编排（LangGraph StateGraph，替代 create_react_agent 黑盒）。

图结构（面试可画图讲解）：
    START → [agent 节点] --有 tool_calls--> [tools 节点] --→ 回 agent
                         --无 tool_calls--> END
    tools 节点内部做轻量反思：SQL/查询工具失败次数达到上限时，
    注入 SystemMessage 强制 LLM 停止调用工具、基于已有信息回答（防死循环 + 别乱来）。

对比 create_react_agent（v2）：
- 节点/边/状态全部显式可观测，可打日志、可注入自定义状态（sql_attempts）
- agent 节点用 llm.stream 逐 token 产出（SSE 打字机真实生效；v2 用 invoke 只出完整消息）
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

# 同一轮里 SQL/查询工具执行失败后，LLM 最多重试次数（到达即强制终结）
MAX_SQL_ATTEMPTS = 3
# 判定为"查询类工具"的名称（用于反思计数）
_QUERY_TOOL_NAMES = ("query_mysql", "query_file")


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    sql_attempts: int  # 本轮 SQL/查询工具调用次数（反思用，防死循环）


def _filter_new_messages(input_messages: list, result_messages: list) -> list:
    """从 graph 结果中提取本轮新生成的消息（保留给 chat_api 使用）。"""
    return result_messages[len(input_messages):]


def make_graph(llm, tools, max_sql_attempts: int = MAX_SQL_ATTEMPTS,
               trace: Optional["TraceCollector"] = None, agent_name: str = "agent",
               plan_runtime=None):
    """构造显式编排的 Agent 图。签名与 create_react_agent 用法兼容（chat_api 无需改动）。

    trace：可选轨迹采集器（多智能体模式下主管/专家共享同一个，
           tools 节点自动埋点，得到全链路工具调用链）。
    agent_name：本图所属 Agent 名称，用于轨迹标注（默认单 Agent 场景为 "agent"）。
    """
    tool_map = {t.name: t for t in tools}
    bound_model = llm.bind_tools(tools)

    # ── 节点 1：agent（LLM 决策，逐 token 流式）─────────────────────────────
    # 注意：必须把 graph 的 config 传给模型调用，langgraph 才能捕获 token
    # 事件（stream_mode="messages" → SSE 逐字打字机）；不传则一次性输出。
    def agent_node(state: AgentState, config) -> dict:
        chunks = list(bound_model.stream(state["messages"], config))
        if not chunks:
            return {"messages": [AIMessage(content="")]}
        merged = chunks[0]
        for c in chunks[1:]:
            merged = merged + c
        # AIMessageChunk 是 AIMessage 子类，直接入状态（tool_calls 原样保留）
        return {"messages": [merged]}

    # ── 节点 2：tools（执行工具调用 + 轻量反思）──────────────────────────────
    def tools_node(state: AgentState) -> dict:
        last = state["messages"][-1]
        tool_calls = getattr(last, "tool_calls", None) or []
        outs: list = []
        for tc in tool_calls:
            fn = tool_map.get(tc.get("name"))
            entry = None
            if plan_runtime is not None:
                plan_runtime.before_tool(tc.get("name") or "?", tc.get("args") or {})
            if trace is not None:
                entry = trace.begin(agent_name, tc.get("name") or "?",
                                    tc.get("args") or {})
            if fn is None:
                outs.append(ToolMessage(
                    content=f"错误：未知工具「{tc.get('name')}」，请使用可用工具。",
                    tool_call_id=tc.get("id", ""), name=tc.get("name")))
                if entry is not None:
                    trace.end(entry, "错误：未知工具", status="error")
                continue
            try:
                result = fn.invoke(tc.get("args") or {})
                result_text = str(result)
                issues = plan_runtime.after_tool(tc.get("name") or "?", tc.get("args") or {}, result_text) if plan_runtime else []
                if issues:
                    result_text += "\n【结果质量校验】" + "；".join(issues) + "。请勿生成确定性结论。"
                outs.append(ToolMessage(content=result_text, tool_call_id=tc.get("id", ""), name=tc.get("name")))
                if entry is not None:
                    trace.end(entry, result, status="ok")
            except Exception as e:  # noqa: BLE001 —— 工具异常统一转 ToolMessage，交给 LLM 反思
                outs.append(ToolMessage(
                    content=f"工具执行异常：{e}", tool_call_id=tc.get("id", ""), name=tc.get("name")))
                if plan_runtime is not None:
                    plan_runtime.after_tool(tc.get("name") or "?", tc.get("args") or {}, f"工具执行异常：{e}")
                if entry is not None:
                    trace.end(entry, f"工具执行异常：{e}", status="error")

        # 轻量反思：查询类工具失败/被调用次数达上限 → 强制停止查询，基于已有信息回答
        attempts = int(state.get("sql_attempts", 0))
        if any(tc.get("name") in _QUERY_TOOL_NAMES for tc in tool_calls):
            attempts += 1
        update: dict = {"messages": outs}
        if attempts >= max_sql_attempts:
            update["sql_attempts"] = attempts
            update["messages"] = outs + [SystemMessage(
                content=f"注意：查询工具已连续尝试 {max_sql_attempts} 次。请停止调用任何查询工具，"
                        f"直接根据已掌握的信息回答用户，并如实说明查询遇到的问题。")]
        else:
            update["sql_attempts"] = attempts
        return update

    # ── 条件边：agent 有 tool_calls → 走 tools；否则 → END ───────────────────
    def should_continue(state: AgentState) -> Literal["tools", "__end__"]:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()
