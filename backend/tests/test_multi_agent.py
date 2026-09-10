"""P1 分析角色：角色注册 / 验证门禁 / 子图执行 / 主管路由。"""
import time
import threading

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.agent.multi_agent import (
    _invoke_with_timeout,
    make_agent,
    SQL_TOOL_NAMES,
    VIZ_TOOL_NAMES,
    FILE_TOOL_NAMES,
    STAT_TOOL_NAMES,
    WorkerTimeoutError,
)
from app.agent.analysis_plan import AnalysisPlan, PlanRuntime
from app.agent.prompts import SUPERVISOR_PROMPT
from app.tools.agent_tools import make_tools


# ── fake LLM：按顺序消费 responses（主管与专家共享同一实例，调用顺序即消费顺序）──
class FakeLLM(BaseChatModel):
    responses: list = []
    last_bound_tools: list = []
    _i: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        self.last_bound_tools = list(tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        r = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        return ChatResult(generations=[ChatGeneration(message=r)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk
        r = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        yield ChatGenerationChunk(message=AIMessageChunk(content=r.content, tool_calls=r.tool_calls or []))


def _tool_call(name, args, cid="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def _all_tools():
    import pandas as pd
    return make_tools(None, files={"成绩.csv": pd.DataFrame({"a": [1, 2]})})


# ── 专家分组 ──────────────────────────────────────────────────────────────────
def test_tool_name_groups_cover_all():
    names = {t.name for t in _all_tools()}
    grouped = set(SQL_TOOL_NAMES) | set(VIZ_TOOL_NAMES) | set(FILE_TOOL_NAMES) | set(STAT_TOOL_NAMES)
    assert names == grouped  # 所有工具都归属某个专家，无遗漏


def test_make_agent_registers_analysis_roles():
    llm = FakeLLM(responses=[AIMessage(content="ok")])
    graph, prompt = make_agent(llm, _all_tools())
    assert prompt == SUPERVISOR_PROMPT
    role_names = [t.name for t in llm.last_bound_tools]
    assert role_names == ["data_executor", "statistical_validator", "insight_writer"]


def test_make_agent_keeps_data_role_without_files():
    llm = FakeLLM(responses=[AIMessage(content="ok")])
    # 无上传文件时，数据执行角色仍负责 MySQL；角色边界不随数据源消失。
    graph, prompt = make_agent(llm, make_tools(None, files={}))
    role_names = [t.name for t in llm.last_bound_tools]
    assert role_names == ["data_executor", "statistical_validator", "insight_writer"]


# ── 主管路由 + 专家执行（顺序消费 FakeLLM responses）─────────────────────────
def test_supervisor_routes_data_validate_then_insight():
    llm = FakeLLM(responses=[
        _tool_call("data_executor", {"request": "查询订单数量"}, "c1"),
        _tool_call("query_mysql", {"sql": "SELECT COUNT(*) FROM t"}, "c2"),
        AIMessage(content="已取得订单证据：共 120 单"),
        _tool_call("statistical_validator", {"request": "检查订单证据"}, "c3"),
        _tool_call("insight_writer", {"request": "基于已通过验证的订单证据回答"}, "c4"),
        AIMessage(content="上月订单数为 120 单，口径为订单记录数。"),
        AIMessage(content="上月共 **120** 单。"),
    ])
    graph, _ = make_agent(llm, _all_tools())
    result = graph.invoke({"messages": [HumanMessage(content="上月订单数？")]})
    msgs = result["messages"]
    assert msgs[-1].content == "上月共 **120** 单。"
    # 专家子图执行过（存在 query_mysql 的 ToolMessage）
    assert any("120" in m.content for m in msgs if m.type == "tool")


def test_supervisor_routes_viz_expert():
    llm = FakeLLM(responses=[
        _tool_call("insight_writer", {"request": "画柱状图，数据 A:10 B:20"}, "c1"),
        _tool_call("make_chart", {"chart_type": "bar", "title": "对比", "x_labels": ["A", "B"],
                                  "series": [{"name": "s", "data": [10, 20]}]}, "c2"),
        AIMessage(content="已生成柱状图"),
        AIMessage(content="已为您生成柱状图。"),
    ])
    graph, _ = make_agent(llm, _all_tools())
    result = graph.invoke({"messages": [HumanMessage(content="画个柱状图")]})
    assert result["messages"][-1].content == "已为您生成柱状图。"


def test_supervisor_chitchat_no_expert_call():
    llm = FakeLLM(responses=[
        AIMessage(content="你好！有什么可以帮你？"),  # 主管直接回答，不调专家
    ])
    graph, _ = make_agent(llm, _all_tools())
    result = graph.invoke({"messages": [HumanMessage(content="你好")]})
    msgs = result["messages"]
    assert len(msgs) == 2  # 只有输入 + 回答，没有专家调用
    assert msgs[-1].content == "你好！有什么可以帮你？"


# ── 回退：专家太少 → 单 Agent ────────────────────────────────────────────────
def test_fallback_when_no_tools():
    llm = FakeLLM(responses=[AIMessage(content="hi")])
    graph, prompt = make_agent(llm, [])  # 空工具 → 单 agent
    assert prompt == SUPERVISOR_PROMPT  # prompt 仍返回（调用方统一使用）
    result = graph.invoke({"messages": [HumanMessage(content="hi")]})
    assert result["messages"][-1].content == "hi"


def test_worker_timeout_is_bounded():
    started = time.monotonic()

    with pytest.raises(WorkerTimeoutError):
        _invoke_with_timeout(lambda: time.sleep(0.2), 0.01)

    assert time.monotonic() - started < 0.15


def test_timed_out_worker_keeps_slot_until_background_thread_finishes():
    semaphore = threading.BoundedSemaphore(1)
    assert semaphore.acquire(blocking=False)

    with pytest.raises(WorkerTimeoutError):
        _invoke_with_timeout(
            lambda: time.sleep(0.1),
            0.01,
            on_complete=semaphore.release,
        )

    assert not semaphore.acquire(blocking=False)
    time.sleep(0.15)
    assert semaphore.acquire(blocking=False)
    semaphore.release()


def test_worker_timeout_marks_analysis_unusable():
    runtime = PlanRuntime(
        AnalysisPlan(goal="统计订单", metrics=["订单数"]),
        "统计订单",
    )
    runtime.record_worker_failure("data_executor", "timeout", "超过 1 秒")
    report = runtime.validate_evidence()

    assert runtime.validation_status == "rejected"
    assert report["status"] == "rejected"
    assert any("data_executor执行超时" in issue for issue in report["issues"])


def test_busy_worker_is_not_queued_and_marks_plan_unusable():
    runtime = PlanRuntime(AnalysisPlan(goal="统计订单", metrics=["订单数"]), "统计订单")
    semaphore = threading.BoundedSemaphore(1)
    assert semaphore.acquire(blocking=False)
    llm = FakeLLM(responses=[
        _tool_call("data_executor", {"request": "查询订单"}, "c1"),
        AIMessage(content="当前资源繁忙，请稍后重试。"),
    ])
    graph, _ = make_agent(
        llm,
        _all_tools(),
        plan_runtime=runtime,
        worker_semaphore=semaphore,
    )

    result = graph.invoke({"messages": [HumanMessage(content="订单数？")]})
    semaphore.release()

    worker_message = next(message.content for message in result["messages"]
                          if message.type == "tool" and message.name == "data_executor")
    assert worker_message.startswith("[WORKER_BUSY]")
    assert any("data_executor资源繁忙" in issue for issue in runtime.plan.quality_issues)


# ── 图表标记回传（前端渲染的关键）────────────────────────────────────────────
def test_viz_expert_forwards_chart_markup():
    """洞察角色最终回复没带 CHART 标记时，标记也必须回传主管层。"""
    llm = FakeLLM(responses=[
        _tool_call("insight_writer", {"request": "画柱状图"}, "c1"),
        _tool_call("make_chart", {"chart_type": "bar", "title": "对比",
                                  "x_labels": ["A"], "series": [{"name": "s", "data": [1]}]}, "c2"),  # 专家 → 图表工具
        AIMessage(content="已生成柱状图"),                                   # 专家回复（无标记）
        AIMessage(content="已为您生成柱状图。"),                             # 主管最终回复
    ])
    graph, _ = make_agent(llm, _all_tools())
    result = graph.invoke({"messages": [HumanMessage(content="画个图")]})
    # 主管图里应存在含 CHART 标记的 ToolMessage（viz_expert 返回值）
    chart_found = any("<!--CHART:" in (m.content or "") for m in result["messages"])
    assert chart_found, "专家子图的 CHART 标记必须回传到主管层，否则前端无图"
    assert result["messages"][-1].content == "已为您生成柱状图。"


def test_insight_writer_is_blocked_before_validation():
    """即使 Supervisor 误路由，表达角色也不能在验证前生成洞察。"""
    from app.agent.analysis_plan import AnalysisPlan, PlanRuntime

    runtime = PlanRuntime(AnalysisPlan(goal="统计订单"), "统计订单")
    llm = FakeLLM(responses=[
        _tool_call("insight_writer", {"request": "直接给订单结论"}, "c1"),
        AIMessage(content="已停止，等待统计验证。"),
    ])
    graph, _ = make_agent(llm, _all_tools(), plan_runtime=runtime)
    result = graph.invoke({"messages": [HumanMessage(content="统计订单")]})
    assert runtime.validation_status == "pending"
    assert "验证尚未通过" in result["messages"][2].content


def test_validator_subgraph_uses_python_statistical_tool(monkeypatch):
    """有真实证据时，验证角色可把趋势计算交给统一统计工具。"""
    import pandas as pd
    from app.agent.analysis_plan import AnalysisPlan, PlanRuntime, PlanStep

    monkeypatch.setattr(pd, "read_sql", lambda sql, engine: pd.DataFrame({
        "month": ["2025-01-01", "2025-02-01", "2025-03-01"],
        "sales": [10, 20, 30],
    }))
    runtime = PlanRuntime(AnalysisPlan(
        goal="分析销售趋势", metrics=["销售额"],
        steps=[PlanStep(id="q", title="查询", kind="query"),
               PlanStep(id="v", title="验证", kind="validate")],
    ), "趋势")
    llm = FakeLLM(responses=[
        _tool_call("data_executor", {"request": "查询趋势"}, "c1"),
        _tool_call("query_mysql", {"sql": "SELECT month, sales FROM t"}, "c2"),
        AIMessage(content="已取得 month 和 sales 证据。"),
        _tool_call("statistical_validator", {"request": (
            "请用真实证据计算趋势，records=[{\"month\":\"2025-01-01\",\"sales\":10},"
            "{\"month\":\"2025-02-01\",\"sales\":20},{\"month\":\"2025-03-01\",\"sales\":30}]，"
            "调用 analyze_trend_tool。"
        )}, "c3"),
        _tool_call("analyze_trend_tool", {"data": [
            {"month": "2025-01-01", "sales": 10},
            {"month": "2025-02-01", "sales": 20},
            {"month": "2025-03-01", "sales": 30},
        ], "period_col": "month", "value_col": "sales"}, "c4"),
        AIMessage(content="趋势统计已完成，斜率为 10。"),
        _tool_call("insight_writer", {"request": "基于验证报告给出趋势结论"}, "c5"),
        AIMessage(content="销售额呈上升趋势。"),
        AIMessage(content="销售额呈上升趋势。"),
    ])
    graph, _ = make_agent(llm, _all_tools(), plan_runtime=runtime)
    result = graph.invoke({"messages": [HumanMessage(content="销售趋势？")]})
    assert runtime.validation_status == "approved"
    validator_result = next(message.content for message in result["messages"]
                            if message.type == "tool" and message.name == "statistical_validator")
    assert "趋势统计已完成" in validator_result
    assert '"sample_size"' in validator_result and '"intermediate"' in validator_result
