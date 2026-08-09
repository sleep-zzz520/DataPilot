"""有限 HITL：风险判断、一次性确认和拒绝后的不可重放。"""

from app.agent.analysis_plan import AnalysisPlan
from app.agent.hitl import assess_confirmation
from app.api.chat_api import ChatRequest, _resolve_plan
from app.persistence import consume_pending_approval, save_pending_approval


def test_normal_analysis_does_not_pause():
    plan = AnalysisPlan(goal="统计订单", metrics=["订单数"], steps=[
        {"id": "q", "title": "查询订单", "kind": "query"},
        {"id": "v", "title": "校验结果", "kind": "validate"},
    ])
    assert assess_confirmation("上个月订单数", plan)["required"] is False


def test_export_and_sensitive_request_returns_explainable_confirmation():
    plan = AnalysisPlan(goal="导出客户联系方式", data_sources=["customers"],
                        metrics=["手机号"], output_format="table", steps=[
                            {"id": "q", "title": "查询客户", "kind": "query"},
                        ])
    result = assess_confirmation("导出客户手机号为 CSV", plan)
    assert result["required"] is True
    assert {"sensitive_data", "data_export"} <= set(result["reasons"])
    assert result["estimated_cost"]["token_range"]
    assert result["data_scope"]
    assert result["steps"]


def test_metric_ambiguity_is_clarification_not_execution():
    plan = AnalysisPlan(goal="统计销售额", clarification_needed=True,
                        clarification_question="按含税还是不含税？", steps=[
                            {"id": "q", "title": "查询销售额", "kind": "query"},
                        ])
    result = assess_confirmation("统计销售额", plan)
    assert result["mode"] == "clarification"
    assert result["prompt"] == "按含税还是不含税？"


def test_pending_approval_is_bound_and_one_time(isolated_storage):
    save_pending_approval("a1", 7, "s1", "hash-1", {"plan": {}, "confirmation": {"reasons": []}})
    assert consume_pending_approval("a1", 7, "s1", "wrong", "approve")["status"] == "invalid"
    assert consume_pending_approval("a1", 7, "s1", "hash-1", "reject")["status"] == "rejected"
    assert consume_pending_approval("a1", 7, "s1", "hash-1", "approve")["status"] == "invalid"


def test_resolve_plan_pauses_then_resumes_saved_plan(isolated_storage, monkeypatch):
    plan = AnalysisPlan(goal="导出客户数据", data_sources=["customers"],
                        output_format="table", steps=[
                            {"id": "q", "title": "查询客户", "kind": "query"},
                        ])
    monkeypatch.setattr("app.api.chat_api._build_plan", lambda *args: (plan, {"type": "plan"}))
    first = ChatRequest(message="导出客户数据为 CSV", session_id="s1", llm_config_id=1, db_config_id=1)
    user = {"uid": 7, "username": "alice"}
    pending = _resolve_plan(first, "s1", object(), object(), None, user)
    assert pending["kind"] == "confirmation"

    approval = pending["confirmation"]["approval_id"]
    monkeypatch.setattr("app.api.chat_api._build_plan", lambda *args: (_ for _ in ()).throw(AssertionError("must reuse plan")))
    resumed = _resolve_plan(
        first.model_copy(update={"approval_id": approval, "approval_decision": "approve"}),
        "s1", object(), object(), None, user,
    )
    assert resumed["kind"] == "ready"
    assert resumed["plan"].goal == plan.goal
