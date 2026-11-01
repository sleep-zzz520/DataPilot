"""Production integration contracts for planning, approvals, streaming and usage."""
import json
from uuid import uuid4
from types import SimpleNamespace
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from app.agent.analysis_plan import make_analysis_plan_graph
from app.api.chat_api import ChatRequest, _memory_scope, _request_hash, _resolve_plan
from app.usage.callback import RequestUsage


def test_planner_receives_memory_as_reference():
    captured = []
    class Planner:
        def invoke(self, messages):
            captured.extend(messages)
            return AIMessage(content=json.dumps({"goal": "净销售额", "steps": [{"id": "q", "title": "查数", "kind": "query"}]}))
    result = make_analysis_plan_graph(Planner()).invoke({"question": "按上次口径统计",
        "catalog": [], "memory_context": "已确认sales减refund", "plan": None, "error": None})
    assert "sales减refund" in captured[0].content
    assert "当前用户要求优先" in captured[0].content
    assert result["plan"].goal == "净销售额"


def test_memory_scope_and_approval_bind_mode_and_source():
    req = ChatRequest(message="统计", llm_config_id=1, db_config_id=2, file_ids=["b", "a"])
    assert _memory_scope(req) == _memory_scope(req.model_copy(update={"file_ids": ["a", "b"]}))
    assert _memory_scope(req) != _memory_scope(req.model_copy(update={"db_config_id": 3}))
    assert _request_hash(req, "s") != _request_hash(req.model_copy(update={"agent_mode": "multi"}), "s")


def test_resolve_plan_uses_scoped_memory_before_planning(isolated_storage, monkeypatch):
    from app.agent.analysis_plan import AnalysisPlan
    seen = []
    monkeypatch.setattr("app.api.chat_api.build_memory_context", lambda *a, **kw: seen.append(kw) or "口径:净额")
    def planner(*args):
        assert args[-1] == "口径:净额"
        return AnalysisPlan(goal="统计", steps=[{"id": "q", "title": "查询", "kind": "query"}]), {}
    monkeypatch.setattr("app.api.chat_api._build_plan", planner)
    req = ChatRequest(message="按上次口径", llm_config_id=1, db_config_id=2)
    assert _resolve_plan(req, "s", object(), None, None, {"uid": 1})["kind"] == "ready"
    assert seen == [{"scope": _memory_scope(req)}]


def test_usage_counts_nested_and_maintenance_once():
    counter = RequestUsage()
    response = LLMResult(generations=[[ChatGeneration(message=AIMessage(content="ok",
        usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}))]])
    for run in (uuid4(), uuid4(), uuid4()):
        counter.on_llm_end(response, run_id=run)
        counter.on_llm_end(response, run_id=run)
    assert (counter.calls, counter.input_tokens, counter.output_tokens) == (3, 21, 9)


def test_usage_late_completion_after_response_is_persisted():
    counter = RequestUsage()
    written = []
    counter.finalize(lambda i, o: written.append((i, o)))
    response = LLMResult(generations=[[ChatGeneration(message=AIMessage(content="late",
        usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}))]])
    run_id = uuid4()
    counter.on_llm_end(response, run_id=run_id)
    counter.on_llm_end(response, run_id=run_id)
    counter.finalize(lambda i, o: written.append((i, o)))
    assert written == [(7, 3)]
