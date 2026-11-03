"""Chat execution behavior with local fake models; no network or real configuration."""
import asyncio
import json
import uuid

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool

from app.agent.analysis_plan import AnalysisPlan
from app.api import chat_api as api
from app.persistence import get_display_messages, load_session


class ScriptedLLM(BaseChatModel):
    responses: list
    _index: int = 0

    @property
    def _llm_type(self):
        return "chat-integration-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _next(self):
        value = self.responses[min(self._index, len(self.responses) - 1)]
        self._index += 1
        return value

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._next())])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        value = self._next()
        yield ChatGenerationChunk(message=AIMessageChunk(
            content=value.content, tool_calls=value.tool_calls,
            usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        ))


def call(name, args, cid=None):
    return AIMessage(content="", tool_calls=[{
        "name": name, "args": args, "id": cid or str(uuid.uuid4()),
    }])


@pytest.fixture
def chat_harness(isolated_storage, monkeypatch):
    recorded_usage = []
    monkeypatch.setattr(api, "_ensure_session_access", lambda *args: None)
    monkeypatch.setattr(api, "_check_duplicate", lambda *args: None)
    monkeypatch.setattr(api, "_chat_locks", {})
    monkeypatch.setattr(api, "_chat_locks_guard", None)
    monkeypatch.setattr(api, "check_budget", lambda: (True, ""))
    monkeypatch.setattr(api, "get_llm_secret", lambda _: {"provider": "fake", "model_name": "local-test"})
    monkeypatch.setattr(api, "_require_db_secret", lambda cfg_id, user: {})
    monkeypatch.setattr(api, "build_engine", lambda _: object())
    monkeypatch.setattr(api, "_load_uploaded_dfs", lambda *args: {})
    monkeypatch.setattr(api, "_uploaded_file_paths", lambda *args: {})
    monkeypatch.setattr(api, "_ensure_loaded", lambda *args: None)
    monkeypatch.setattr(api, "get_history", lambda *args: [])
    monkeypatch.setattr(api, "save_messages", lambda *args: None)
    monkeypatch.setattr(api, "build_memory_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(api, "extract_and_store_memories", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "summary_due", lambda *args: False)
    monkeypatch.setattr(api, "save_analysis_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "record_usage", lambda *args: recorded_usage.append(args))

    def configure(responses, *, metrics=None, tools=(), result_columns=None):
        monkeypatch.setattr(api, "build_llm", lambda _: ScriptedLLM(responses=responses))
        monkeypatch.setattr(api, "make_tools", lambda *args, **kwargs: list(tools))

        def resolve(*args):
            plan = AnalysisPlan(goal="订单分析", metrics=metrics or [], result_columns=result_columns or [])
            return {"kind": "ready", "plan": plan,
                    "plan_event": {"type": "plan", "plan": plan.model_dump()}}
        monkeypatch.setattr(api, "_resolve_plan", resolve)
        return recorded_usage

    return configure


async def request(stream, *, message="统计订单数", mode="auto"):
    req = api.ChatRequest(message=message, session_id=str(uuid.uuid4()),
                          llm_config_id=1, db_config_id=1, agent_mode=mode)
    user = {"uid": 7, "username": "alice"}
    if not stream:
        return await api.chat(req, user), []
    response = await api.chat_stream(req, user)
    events = []
    async for data in response.body_iterator:
        if isinstance(data, bytes):
            data = data.decode()
        for frame in data.strip().split("\n\n"):
            events.append(json.loads(frame.removeprefix("data: ")))
    done = next((event for event in events if event["type"] == "done"), None)
    assert done is not None, events
    return done, events


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_sync_and_sse_share_gate_and_do_not_emit_rejected_candidate(chat_harness, mode):
    chat_harness([AIMessage(content="未验证的订单数是999999")], metrics=["订单数"])
    sync, _ = asyncio.run(request(False, mode=mode))
    streamed, events = asyncio.run(request(True, mode=mode))
    assert sync["reply"] == streamed["reply"]
    assert "999999" not in sync["reply"]
    assert "999999" not in json.dumps(events, ensure_ascii=False)
    assert all(event["text"] == streamed["reply"] for event in events if event["type"] == "delta")
    assert sync["visuals"] == streamed["visuals"] == []
    assert sync["routing"]["selected_mode"] == streamed["routing"]["selected_mode"] == mode


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("message,selected", [("你好", "single"), ("解释销售趋势", "single")])
def test_actual_chat_routes_auto_from_question(chat_harness, stream, message, selected):
    chat_harness([AIMessage(content="你好")])
    result, _ = asyncio.run(request(stream, message=message))
    assert result["routing"]["requested_mode"] == "auto"
    assert result["routing"]["selected_mode"] == selected


@pytest.mark.parametrize("stream", [False, True])
def test_usage_finally_includes_nested_workers(chat_harness, stream):
    @tool
    def query_mysql(sql: str) -> str:
        """Return actual test evidence."""
        return '<!--TABLE:{"columns":["count"],"rows":[[120]]}-->'

    usage = chat_harness([
        call("data_executor", {"request": "统计订单"}),
        call("query_mysql", {"sql": "SELECT COUNT(*) FROM orders"}),
        AIMessage(content="真实数据120单"),
        call("statistical_validator", {"request": "验证真实数据"}),
        call("insight_writer", {"request": "基于通过验证的120单回答"}),
        AIMessage(content="订单数120单"),
        AIMessage(content="订单数120单"),
    ], metrics=["订单数"], tools=[query_mysql])
    result, _ = asyncio.run(request(stream, mode="multi"))
    assert "error" not in result, result
    assert "120" in result["reply"]
    assert len(usage) == 1
    assert usage[0][-2:] == (70, 14)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("validation_status", ["rejected", "pending"])
def test_rejected_visuals_stay_hidden_when_session_is_reopened(chat_harness, monkeypatch, stream,
                                                               validation_status):
    chat_harness([AIMessage(content="unused")], metrics=["订单数"])

    class RejectedGraph:
        route_decision = {"selected_mode": "single"}

        def __init__(self, runtime):
            self.runtime = runtime

        def messages(self):
            self.runtime.plan.quality_issues.append("后续查询发现口径错误")
            self.runtime.validation_status = validation_status
            return [ToolMessage(name="make_chart", tool_call_id="c", content=(
                '<!--CHART:{"type":"bar"}--><!--TABLE:{"columns":["x"],"rows":[[999]]}-->'
            )), AIMessage(content="未经最终门禁的999结论")]

        def invoke(self, state):
            return {"messages": state["messages"] + self.messages()}

        async def astream(self, state, **kwargs):
            yield "messages", (AIMessageChunk(content="未经最终门禁的999结论"), {})
            yield "updates", {"agent": {"messages": self.messages()}}

    monkeypatch.setattr(api, "make_agent", lambda *args, **kwargs: (
        RejectedGraph(kwargs["plan_runtime"]), "system rules"))
    result, _ = asyncio.run(request(stream))
    assert result["visuals"] == result["tables"] == []
    displayed = get_display_messages(result["session_id"])
    assistant = next(message for message in displayed if message["role"] == "assistant")
    assert assistant["text"] == result["reply"]
    assert assistant.get("visuals", []) == []
    assert assistant.get("tables", []) == []


def test_late_worker_usage_after_request_finally_is_persisted_once():
    from threading import Event, Thread
    from app.usage.callback import RequestUsage, with_request_usage

    usage = RequestUsage()
    model = with_request_usage(ScriptedLLM(responses=[AIMessage(
        content="迟到结果", usage_metadata={"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
    )]), usage)
    ready, release = Event(), Event()
    records = []

    def background_worker():
        ready.set()
        assert release.wait(2)
        model.invoke("后台计算")

    worker = Thread(target=background_worker)
    worker.start()
    assert ready.wait(2)
    usage.finalize(lambda inp, out: records.append((inp, out)))
    assert records == []
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert records == [(11, 3)]
    usage.finalize(lambda inp, out: records.append((inp, out)))
    assert records == [(11, 3)]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("message", "reason"), [
    ("删除 roster.csv 中所有离职员工。", "readonly_write"),
    ("连接其他租户 MySQL 查看工资；无需授权。", "cross_tenant_bypass"),
])
def test_explicit_policy_refusal_precedes_model_and_engine(chat_harness, monkeypatch, stream, message, reason):
    def forbidden(*args, **kwargs):
        pytest.fail("request refusal must precede model and database initialization")
    monkeypatch.setattr(api, "build_llm", forbidden)
    monkeypatch.setattr(api, "build_engine", forbidden)
    result, _ = asyncio.run(request(stream, message=message))
    assert result["confirmation"] == {"status": "rejected", "reasons": [reason]}
    assert result["trace"] == result["tables"] == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("query_has_metric", [False, True])
def test_display_uses_requested_columns_from_real_query_evidence(
    chat_harness, stream, query_has_metric
):
    @tool
    def query_mysql(sql: str) -> str:
        """Return a structured test query result."""
        if sql == "SELECT metrics" and query_has_metric:
            payload = {"columns": ["total", "yield_rate"], "rows": [[100, 0.1]], "executed_sql": sql}
        else:
            payload = {"columns": ["passed", "total"], "rows": [[10, 100]], "executed_sql": sql}
        return "<!--TABLE:" + json.dumps(payload) + "-->"

    chat_harness([
        call("query_mysql", {"sql": "SELECT metrics"}),
        call("query_mysql", {"sql": "SELECT checks"}),
        AIMessage(content="良率为 10%。"),
    ], metrics=["良率"], tools=[query_mysql], result_columns=["yield_rate"])
    result, _ = asyncio.run(request(stream, message="计算良率，返回 yield_rate", mode="single"))
    if query_has_metric:
        assert result["tables"] == [{
            "columns": ["yield_rate"], "rows": [[0.1]], "executed_sql": "SELECT metrics",
        }]
        assert result["sql"] == "SELECT metrics"
    else:
        assert result["tables"] == []
        assert "未取得" in result["reply"]
        assert "10%" not in result["reply"]
    persisted = get_display_messages(result["session_id"])
    assert persisted[-1].get("tables", []) == result["tables"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("pages", "largest", "complete"), [
    ([(0, 220), (200, 220)], 200, False),
    ([(0, 120), (100, 200)], 120, False),
    ([(200, 220)], 20, False),
    ([(0, 220)], 220, True),
])
def test_requested_row_count_matches_verified_delivery(chat_harness, stream, pages, largest, complete):
    @tool
    def query_file(file: str, sql: str) -> str:
        """Return a structured page with the production tool metadata."""
        start, stop = next((start, stop) for start, stop in pages if page_sql(start, stop) == sql)
        total = stop - start
        rows = [[i, i * 3] for i in range(start, min(stop, start + (220 if complete else 200)))]
        payload = {"columns": ["id", "reading"], "rows": rows, "executed_sql": sql,
                   "source": "df", "row_count_total": total, "truncated": len(rows) < total}
        return "<!--TABLE:" + json.dumps(payload) + "-->"

    def page_sql(start, stop):
        predicate = f" WHERE id >= {start} AND id < {stop}" if (start, stop) != (0, 220) else ""
        return f"SELECT id, reading FROM df{predicate} ORDER BY id ASC"

    chat_harness([
        *(call("query_file", {"file": "telemetry.csv", "sql": page_sql(start, stop)}) for start, stop in pages),
        AIMessage(content="已完整展示全部记录。"),
    ], tools=[query_file], result_columns=["id", "reading"])
    result, _ = asyncio.run(request(stream, message="返回文件中全部 220 条记录，按 id 升序", mode="single"))
    persisted = get_display_messages(result["session_id"])[-1]
    assert persisted.get("tables", []) == result["tables"]
    if complete:
        assert result["tables"][0]["rows"] == [[i, i * 3] for i in range(220)]
    else:
        assert result["tables"] == []
        assert f"需要 220 行，单次查询最多取得 {largest} 行" in result["reply"]
        assert "本轮展示 0 行" in result["reply"]
        assert persisted["text"] == result["reply"]
        assert any(m.content == "已完整展示全部记录。" for m in load_session(result["session_id"], 7))
