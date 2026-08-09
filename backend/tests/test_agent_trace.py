"""Agent 轨迹采集（agent/trace.py + graph/multi_agent 埋点）单测。"""
import asyncio

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool

from app.agent.trace import TraceCollector, summarize
from app.agent.graph import make_graph
from app.agent.multi_agent import make_agent


# ── fake LLM：与 test_multi_agent 同款，主管/专家共享实例按序消费 ────────────
class FakeLLM(BaseChatModel):
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
        return ChatResult(generations=[ChatGeneration(message=r)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        r = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=r.content, tool_calls=r.tool_calls or []))


def _answer(content: str) -> AIMessage:
    return AIMessage(content=content)


def _tool_call(name: str, args: dict, cid: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


@tool
def add(a: int, b: int) -> int:
    """加法"""
    return a + b


@tool
def boom() -> str:
    """总抛异常"""
    raise RuntimeError("爆炸了")


# ── summarize：入参/结果摘要 ─────────────────────────────────────────────────
def test_summarize_dict_and_single_line():
    s = summarize({"sql": "SELECT 1\nFROM t", "limit": 10})
    assert "\n" not in s and "SELECT 1" in s and "limit" in s


def test_summarize_truncates_long_text():
    s = summarize("x" * 1000)
    assert len(s) <= 400 and "共 1000 字符" in s


def test_summarize_none_empty():
    assert summarize(None) == ""


# ── TraceCollector：基础行为 ─────────────────────────────────────────────────
def test_collector_begin_end_ok():
    c = TraceCollector()
    e = c.begin("supervisor", "query_mysql", {"sql": "SELECT 1"})
    c.end(e, "1 行", status="ok")
    assert len(c.entries) == 1
    entry = c.entries[0]
    assert entry["seq"] == 1 and entry["agent"] == "supervisor"
    assert entry["tool"] == "query_mysql" and "SELECT 1" in entry["input"]
    assert entry["output"] == "1 行" and entry["status"] == "ok"
    assert entry["duration_ms"] is not None


def test_collector_error_status():
    c = TraceCollector()
    e = c.begin("supervisor", "boom", {})
    c.end(e, "工具执行异常：爆炸了", status="error")
    assert c.entries[0]["status"] == "error"


def test_collector_nesting_depth_and_order():
    c = TraceCollector()
    parent = c.begin("supervisor", "sql_expert", {"request": "查订单"})
    child = c.begin("sql_expert", "query_mysql", {"sql": "SELECT 1"})
    c.end(child, "ok")
    c.end(parent, "完成")
    assert [e["depth"] for e in c.entries] == [0, 1]
    assert [e["seq"] for e in c.entries] == [1, 2]
    assert c.entries[0]["agent"] == "supervisor"
    assert c.entries[1]["agent"] == "sql_expert"


def test_collector_snapshot_is_copy():
    c = TraceCollector()
    e = c.begin("agent", "add", {"a": 1, "b": 2})
    c.end(e, 3)
    snap = c.snapshot()
    snap.clear()
    assert len(c.entries) == 1


# ── 显式图埋点 ───────────────────────────────────────────────────────────────
def test_graph_records_trace_on_tool_call():
    llm = FakeLLM(responses=[
        _tool_call("add", {"a": 1, "b": 2}),
        _answer("结果是 3"),
    ])
    c = TraceCollector()
    graph = make_graph(llm, tools=[add], trace=c, agent_name="supervisor")
    graph.invoke({"messages": [HumanMessage(content="1+2=?")]})
    assert len(c.entries) == 1
    e = c.entries[0]
    assert e["agent"] == "supervisor" and e["tool"] == "add"
    assert e["input"] == '{"a": 1, "b": 2}' and e["output"] == "3"
    assert e["status"] == "ok"


def test_graph_trace_unknown_tool_error():
    llm = FakeLLM(responses=[
        _tool_call("不存在的工具", {}),
        _answer("抱歉"),
    ])
    c = TraceCollector()
    graph = make_graph(llm, tools=[add], trace=c)
    graph.invoke({"messages": [HumanMessage(content="x")]})
    assert c.entries[0]["tool"] == "不存在的工具"
    assert c.entries[0]["status"] == "error"


def test_graph_trace_tool_exception_error():
    llm = FakeLLM(responses=[
        _tool_call("boom", {}),
        _answer("出错了"),
    ])
    c = TraceCollector()
    graph = make_graph(llm, tools=[boom], trace=c)
    graph.invoke({"messages": [HumanMessage(content="x")]})
    e = c.entries[0]
    assert e["status"] == "error" and "爆炸了" in e["output"]


# ── 流式：astream 期间增量产出轨迹（SSE trace 事件的核心逻辑）────────────────
def test_astream_emits_trace_deltas():
    llm = FakeLLM(responses=[
        _tool_call("add", {"a": 1, "b": 2}),
        _answer("结果是 3"),
    ])
    c = TraceCollector()
    graph = make_graph(llm, tools=[add], trace=c, agent_name="supervisor")
    batches: list = []

    async def run():
        sent = 0
        async for mode, data in graph.astream(
            {"messages": [HumanMessage(content="1+2=?")]},
            stream_mode=["messages", "updates"],
        ):
            if mode == "updates":
                entries = c.snapshot()
                if len(entries) > sent:
                    batches.append(entries[sent:])
                    sent = len(entries)

    asyncio.run(run())
    # 工具节点完成后增量出现 add 轨迹（与 chat_stream 的推送逻辑一致）
    assert [e["tool"] for batch in batches for e in batch] == ["add"]
    assert batches[0][0]["status"] == "ok"


# ── 多智能体：主管 + 专家全链路轨迹 ──────────────────────────────────────────
def test_multi_agent_nested_trace():
    llm = FakeLLM(responses=[
        _tool_call("data_executor", {"request": "查询订单数量"}, "c1"),
        _tool_call("query_mysql", {"sql": "SELECT COUNT(*) FROM t"}, "c2"),
        _answer("已取得查询证据：共 120 单"),
        _tool_call("statistical_validator", {"request": "检查证据"}, "c3"),
        _tool_call("insight_writer", {"request": "表达已验证证据"}, "c4"),
        _answer("查询完成：共 120 单"),
        _answer("上月共 **120** 单。"),
    ])
    c = TraceCollector()
    graph, _ = make_agent(llm, _all_tools(), trace=c)
    graph.invoke({"messages": [HumanMessage(content="上月订单数？")]})
    # 主管入口 + 专家内部工具，链路完整且层级正确
    assert [(e["agent"], e["tool"], e["depth"], e["status"]) for e in c.entries] == [
        ("supervisor", "data_executor", 0, "ok"),
        ("data_executor", "query_mysql", 1, "ok"),
        ("supervisor", "statistical_validator", 0, "ok"),
        ("supervisor", "insight_writer", 0, "ok"),
    ]
    assert "SELECT COUNT(*) FROM t" in c.entries[1]["input"]
    assert "120" in c.entries[1]["output"] or "120" in c.entries[0]["output"]


def test_fallback_single_agent_trace_label():
    llm = FakeLLM(responses=[
        _tool_call("add", {"a": 1, "b": 2}),
        _answer("3"),
    ])
    c = TraceCollector()
    graph, _ = make_agent(llm, [add], trace=c)  # 无 SQL/VIZ/FILE 工具 → 回退单 Agent
    graph.invoke({"messages": [HumanMessage(content="1+2")]})
    assert c.entries[0]["agent"] == "agent"
    assert c.entries[0]["tool"] == "add"


def _all_tools():
    import pandas as pd
    from app.tools.agent_tools import make_tools
    return make_tools(None, files={"成绩.csv": pd.DataFrame({"a": [1, 2]})})
