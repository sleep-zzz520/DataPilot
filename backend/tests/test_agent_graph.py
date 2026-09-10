"""显式 Agent 图编排（agent/graph.py）单测：节点流转/工具执行/反思终结/流式兼容。"""
import asyncio
import time
from threading import Event

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool

from app.agent.graph import make_graph, _filter_new_messages, MAX_SQL_ATTEMPTS


# ── 可复用的 fake LLM：响应序列逐轮消费，支持 invoke(完整消息) 与 stream(逐 token) ──
class FakeLLM(BaseChatModel):
    """responses 每项：AIMessage（invoke 返回 / stream 单块）或 [AIMessageChunk, ...]（stream 逐块）。"""
    responses: list = []
    _i: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        r = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        if isinstance(r, list):  # chunk 序列 → 合并成完整消息
            merged = r[0]
            for c in r[1:]:
                merged = merged + c
            r = merged
        return ChatResult(generations=[ChatGeneration(message=r)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        r = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        chunks = r if isinstance(r, list) else [AIMessageChunk(content=r.content, tool_calls=r.tool_calls or [])]
        for c in chunks:
            yield ChatGenerationChunk(message=c)


@tool
def add(a: int, b: int) -> int:
    """加法"""
    return a + b


@tool
def query_mysql(sql: str) -> str:
    """只读查询（测试用，永远失败）"""
    return f"SQL 执行错误：{sql}"


def _answer(content: str) -> AIMessage:
    return AIMessage(content=content)


def _tool_call(name: str, args: dict, cid: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


# ── 基础流转 ──────────────────────────────────────────────────────────────────
def test_no_tool_call_ends_directly():
    llm = FakeLLM(responses=[_answer("你好！")])
    graph = make_graph(llm, tools=[add])
    result = graph.invoke({"messages": [HumanMessage(content="hi")]})
    msgs = result["messages"]
    assert msgs[-1].content == "你好！"
    assert len(msgs) == 2  # input + 回答，无工具中间消息
    new = _filter_new_messages([HumanMessage(content="hi")], msgs)
    assert [m.content for m in new] == ["你好！"]


def test_cancelled_graph_does_not_start_llm_tool_calls():
    llm = FakeLLM(responses=[_tool_call("add", {"a": 1, "b": 2})])
    cancel_event = Event()
    cancel_event.set()
    graph = make_graph(llm, tools=[add], cancel_event=cancel_event)

    result = graph.invoke({"messages": [HumanMessage(content="1+2=?")]})

    assert "执行已取消" in result["messages"][-1].content
    assert not any(isinstance(message, ToolMessage) for message in result["messages"])


def test_expired_deadline_does_not_start_llm_or_tool_calls():
    llm = FakeLLM(responses=[_tool_call("add", {"a": 1, "b": 2})])
    graph = make_graph(llm, tools=[add], deadline=time.monotonic() - 0.01)

    result = graph.invoke({"messages": [HumanMessage(content="1+2=?")]})

    assert "执行已取消" in result["messages"][-1].content
    assert llm._i == 0


def test_tool_call_flow_executes_tool():
    llm = FakeLLM(responses=[
        _tool_call("add", {"a": 1, "b": 2}),
        _answer("结果是 3"),
    ])
    graph = make_graph(llm, tools=[add])
    result = graph.invoke({"messages": [HumanMessage(content="1+2=?")]})
    msgs = result["messages"]
    # input + tool_calls 消息 + ToolMessage(3) + 最终回答
    assert isinstance(msgs[-1], AIMessage) and msgs[-1].content == "结果是 3"
    tool_msg = next(m for m in msgs if isinstance(m, ToolMessage))
    assert tool_msg.content == "3" and tool_msg.name == "add"
    assert result["sql_attempts"] == 0  # add 不是查询工具，不计入反思


def test_unknown_tool_returns_error_message():
    llm = FakeLLM(responses=[
        _tool_call("不存在的工具", {}),
        _answer("抱歉，工具不可用"),
    ])
    graph = make_graph(llm, tools=[add])
    result = graph.invoke({"messages": [HumanMessage(content="x")]})
    tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert "未知工具" in tool_msg.content


def test_tool_exception_is_captured():
    @tool
    def boom() -> str:
        """总抛异常"""
        raise RuntimeError("爆炸了")

    llm = FakeLLM(responses=[
        _tool_call("boom", {}),
        _answer("工具出错了"),
    ])
    graph = make_graph(llm, tools=[boom])
    result = graph.invoke({"messages": [HumanMessage(content="x")]})
    tool_msg = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert "爆炸了" in tool_msg.content  # 异常被转成 ToolMessage 交给 LLM 反思


# ── 反思：SQL 尝试超限强制终结 ────────────────────────────────────────────────
def test_sql_attempts_count_and_force_stop():
    llm = FakeLLM(responses=[
        _tool_call("query_mysql", {"sql": "SELECT 1"}, "c1"),
        _tool_call("query_mysql", {"sql": "SELECT 2"}, "c2"),
        _tool_call("query_mysql", {"sql": "SELECT 3"}, "c3"),
        _answer("查询一直失败，我基于已有信息回答"),
    ])
    graph = make_graph(llm, tools=[query_mysql], max_sql_attempts=MAX_SQL_ATTEMPTS)
    result = graph.invoke({"messages": [HumanMessage(content="查一下")]})
    msgs = result["messages"]
    assert result["sql_attempts"] == 3
    # 第 3 次调用后注入了 SystemMessage 提示停止查询
    assert any(isinstance(m, SystemMessage) and "停止调用" in m.content for m in msgs)
    # LLM 最终按提示直接回答（没有第 4 次工具调用）
    assert msgs[-1].content == "查询一直失败，我基于已有信息回答"


def test_sql_attempts_not_triggered_below_limit():
    llm = FakeLLM(responses=[
        _tool_call("query_mysql", {"sql": "SELECT 1"}, "c1"),
        _answer("完成"),
    ])
    graph = make_graph(llm, tools=[query_mysql], max_sql_attempts=3)
    result = graph.invoke({"messages": [HumanMessage(content="查")]})
    assert result["sql_attempts"] == 1
    assert not any(isinstance(m, SystemMessage) for m in result["messages"])


# ── 流式兼容（SSE 打字机）────────────────────────────────────────────────────
def test_astream_messages_mode_yields_token_chunks():
    llm = FakeLLM(responses=[
        [AIMessageChunk(content="你"), AIMessageChunk(content="好")],
    ])
    graph = make_graph(llm, tools=[add])
    deltas = []
    updates = []
    async def run():
        async for mode, data in graph.astream(
            {"messages": [HumanMessage(content="hi")]}, stream_mode=["messages", "updates"]):
            if mode == "messages":
                chunk, meta = data
                assert isinstance(chunk, AIMessageChunk)
                assert meta.get("langgraph_node") == "agent"
                deltas.append(chunk.content)
            else:
                updates.append(data)
    asyncio.run(run())
    # messages 模式至少产出内容（真实模型为逐 token；FakeLLM 无 streaming events 会合并）
    assert "".join(deltas) == "你好"
    assert updates  # updates 模式也有事件


def test_astream_tool_flow():
    llm = FakeLLM(responses=[
        [AIMessageChunk(content="", tool_calls=[{"name": "add", "args": {"a": 1, "b": 2}, "id": "c1"}])],
        [AIMessageChunk(content="答", ),
         AIMessageChunk(content="案是3")],
    ])
    graph = make_graph(llm, tools=[add])
    done = {}
    async def run():
        async for mode, data in graph.astream(
            {"messages": [HumanMessage(content="1+2")]}, stream_mode=["messages", "updates"]):
            if mode == "messages":
                chunk, _ = data
                done.setdefault("text", "").__add__("")  # 聚合留空（类型检查即可）
            else:
                for node, val in data.items():
                    msgs = val.get("messages") or []
                    if msgs:
                        done[node] = [type(m).__name__ for m in msgs]
    asyncio.run(run())
    assert done.get("tools") == ["ToolMessage"]  # 工具节点产出 ToolMessage
    assert done.get("agent")  # agent 节点有多轮输出
