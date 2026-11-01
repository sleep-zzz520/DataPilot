"""Request-local provenance, dependency invalidation and bounded repair tests."""
import json

import pytest
from langchain_core.messages import AIMessage

from app.agent.analysis_plan import AnalysisPlan, PlanRuntime, make_analysis_plan_graph


def plan_runtime():
    return PlanRuntime(AnalysisPlan(goal="销售额", steps=[
        {"id": "sales", "title": "销售", "kind": "query"},
        {"id": "cost", "title": "成本", "kind": "query"},
        {"id": "growth", "title": "增长", "kind": "calculate", "depends_on": ["sales"]},
        {"id": "chart", "title": "图", "kind": "visualize", "depends_on": ["growth"]},
    ]))


def query(runtime, step="sales", rows=None, sql="SELECT amount FROM sales"):
    args = {"plan_step_id": step, "sql": sql}
    assert runtime.before_tool("query_mysql", args)["allowed"]
    runtime.after_tool("query_mysql", args, "<!--TABLE:" + json.dumps({
        "columns": ["amount"], "rows": rows if rows is not None else [[10], [20]],
        "executed_sql": sql + " LIMIT 500", "source": "mysql",
        "row_count_total": len(rows) if rows is not None else 2, "truncated": False,
    }) + "-->")
    return runtime.plan.steps[0 if step == "sales" else 1].evidence_ids[0]


def test_evidence_contains_real_payload_and_stable_provenance():
    runtime = plan_runtime()
    evidence_id = query(runtime)
    evidence = runtime.get_evidence(evidence_id)
    assert evidence["step_id"] == "sales" and evidence["version"] == 1
    assert evidence["fingerprint"] and evidence["active"]
    assert evidence["payload"]["rows"] == [[10], [20]]
    assert "payload" not in runtime.snapshot()["evidence"][0]
    evidence["payload"]["rows"][0][0] = 999
    assert runtime.evidence_rows([evidence_id])["rows"] == [[10], [20]]
    other_runtime = plan_runtime()
    assert query(other_runtime) == evidence_id


def test_local_invalidation_preserves_independent_branch_and_history():
    runtime = plan_runtime()
    first_id = query(runtime)
    independent_id = query(runtime, "cost")
    runtime.plan.steps[2].status = runtime.plan.steps[3].status = "succeeded"
    assert runtime.invalidate_step("sales") == ["chart", "growth", "sales"]
    assert runtime.plan.steps[1].status == "succeeded"
    assert runtime.evidence_rows([independent_id])["rows"] == [[10], [20]]
    with pytest.raises(ValueError, match="失效"):
        runtime.get_evidence(first_id)
    assert runtime.plan.evidence[0]["active"] is False
    assert runtime.plan.steps[3].output is None


def test_dependency_blocker_has_structured_context():
    runtime = plan_runtime()
    preflight = runtime.preflight_tool("analyze_trend_tool", {"plan_step_id": "growth"})
    assert not preflight["allowed"]
    assert preflight["step_id"] == "growth"
    assert preflight["issues"][0]["code"] == "dependency_not_ready"
    assert preflight["issues"][0]["actual"] == ["sales"]


def test_query_repair_clears_active_issues_but_retains_audit_history():
    runtime = plan_runtime()
    query(runtime, rows=[])
    assert "查询返回空结果" in runtime.plan.quality_issues
    old_evidence = runtime.plan.evidence[0]["id"]
    assert runtime.request_repair("sales", {"sql": "SELECT amount FROM sales WHERE amount > 0"})
    new_evidence = query(runtime, sql="SELECT amount FROM sales WHERE amount > 0")
    assert old_evidence != new_evidence
    assert not runtime.plan.quality_issues
    assert runtime.plan.issue_history[0]["active"] is False
    assert runtime.plan.repair_history[0]["before"]["sql"] != runtime.plan.repair_history[0]["after"]["sql"]
    assert runtime.validate_evidence()["status"] == "approved"


def test_two_repairs_are_the_hard_limit():
    runtime = plan_runtime()
    for attempt in range(3):
        args = {"plan_step_id": "sales", "sql": f"SELECT {attempt}"}
        runtime.before_tool("query_mysql", args)
        runtime.after_tool("query_mysql", args, "SQL 执行错误：temporary connection error")
    assert runtime.plan.steps[0].retry_count == 2
    assert len(runtime.plan.repair_history) == 2
    assert not runtime.needs_repair
    assert not runtime.request_repair("sales", {})
    assert runtime.last_decision["action"] == "stop"


def test_missing_field_requires_clarification_and_cannot_retry():
    runtime = plan_runtime()
    args = {"plan_step_id": "sales", "sql": "SELECT unknown FROM sales"}
    runtime.before_tool("query_mysql", args)
    runtime.after_tool("query_mysql", args, "SQL 执行错误：字段不存在")
    assert runtime.next_action() == "clarify"
    assert not runtime.request_repair("sales", {})
    assert not runtime.should_retry()


def test_budget_stop_is_terminal_and_cannot_resurrect_evidence():
    runtime = plan_runtime()
    query(runtime)
    runtime.record_worker_failure("single", "execution_stopped", "token budget")
    assert not runtime.before_tool("query_mysql", {"sql": "SELECT 1"})["allowed"]
    assert not runtime.request_repair("sales", {})
    runtime.after_tool("query_mysql", {}, '<!--TABLE:{"columns":["a"],"rows":[[9]]}-->')
    assert runtime.validate_evidence()["status"] == "rejected"
    assert len(runtime.plan.evidence) == 1


@pytest.mark.parametrize("steps, phrase", [
    ([{"id": "a", "title": "a", "kind": "query"}, {"id": "a", "title": "b", "kind": "query"}], "重复"),
    ([{"id": "a", "title": "a", "kind": "query", "depends_on": ["b"]},
      {"id": "b", "title": "b", "kind": "query", "depends_on": ["a"]}], "依赖环"),
    ([{"id": "a", "title": "a", "kind": "query", "depends_on": ["missing"]}], "不存在"),
])
def test_runtime_rejects_invalid_dependency_graph(steps, phrase):
    runtime = PlanRuntime(AnalysisPlan(goal="test", steps=steps))
    assert not runtime.preflight_tool("query_mysql", {"sql": "SELECT 1"})["allowed"]
    assert any(phrase in issue for issue in runtime.plan.quality_issues)


def test_planner_json_cannot_inject_trusted_state():
    class Planner:
        def invoke(self, messages):
            return AIMessage(content=json.dumps({"goal": "销售", "steps": [
                {"id": "a", "title": "a", "kind": "query", "status": "succeeded",
                 "evidence_ids": ["fake"]}], "bound_contracts": [{"id": "fake"}],
                "evidence": [{"id": "fake", "row_count": 99}]}))
    result = make_analysis_plan_graph(Planner()).invoke({"question": "销售", "catalog": [], "plan": None, "error": None})
    assert not result["plan"].bound_contracts
    assert not result["plan"].evidence
    assert result["plan"].steps[0].status == "pending"
    assert not result["plan"].steps[0].evidence_ids


def test_truncated_evidence_cannot_feed_statistical_tools():
    runtime = plan_runtime()
    evidence_id = query(runtime)
    runtime.plan.evidence[0]["truncated"] = True
    with pytest.raises(ValueError, match="截断"):
        runtime.evidence_rows([evidence_id])


def contracted_runtime(source="sales", file_name=None):
    from app.agent.contracts import ContractBinding, MetricContract
    runtime = plan_runtime()
    runtime.plan.steps[1].status = "skipped"
    catalog = [{"name": source, "columns": ["amount"]}]
    if file_name:
        catalog[0]["file_name"] = file_name
    runtime.bind_contracts(ContractBinding(contracts=(MetricContract(
        id="sales.total", version="1", formula="SUM(amount)", required_columns=("amount",),
        source=source, unit="CNY", scope="sum booked amount",
    ),)), catalog)
    return runtime


def test_bound_contract_preflight_rejects_wrong_formula_and_accepts_correction():
    runtime = contracted_runtime()
    args = {"plan_step_id": "sales", "sql": "SELECT AVG(amount) FROM sales"}
    first = runtime.preflight_tool("query_mysql", args)
    second = runtime.preflight_tool("query_mysql", args)
    assert not first["allowed"] and first == second
    assert first["issues"][0]["code"] == "METRIC_EXPRESSION_MISMATCH"
    assert len(runtime.plan.active_issues) == len(first["issues"])
    assert runtime.request_repair("sales", {"sql": "SELECT SUM(amount) FROM sales"})
    query(runtime, sql="SELECT SUM(amount) FROM sales")
    assert runtime.validate_evidence()["status"] == "approved"
    assert not runtime.plan.active_issues and runtime.plan.issue_history


@pytest.mark.parametrize("metadata", [
    {"executed_sql": "SELECT AVG(amount) FROM sales"},
    {"source": "different_database"},
])
def test_bound_contract_rejects_mismatched_execution_metadata(metadata):
    runtime = contracted_runtime()
    sql = "SELECT SUM(amount) FROM sales"
    args = {"plan_step_id": "sales", "sql": sql}
    assert runtime.before_tool("query_mysql", args)["allowed"]
    payload = {"columns": ["total"], "rows": [[30]], "executed_sql": sql + " LIMIT 500",
               "source": "mysql", "row_count_total": 1, "truncated": False, **metadata}
    runtime.after_tool("query_mysql", args, "<!--TABLE:" + json.dumps(payload) + "-->")
    assert runtime.validate_evidence()["status"] == "rejected"
    assert runtime.plan.evidence[0]["active"] is False


def test_bound_file_contract_cannot_switch_same_schema_files():
    runtime = contracted_runtime("df", "approved.csv")
    wrong = runtime.preflight_tool("query_file", {
        "plan_step_id": "sales", "sql": "SELECT SUM(amount) FROM df", "file": "other.csv"})
    assert not wrong["allowed"]
    assert any(item["code"] == "file_source_mismatch" for item in wrong["issues"])
    right = runtime.preflight_tool("query_file", {
        "plan_step_id": "sales", "sql": "SELECT SUM(amount) FROM df", "file": "approved.csv"})
    assert right["allowed"]


def test_evidence_tools_require_live_references_and_honor_lineage():
    runtime = contracted_runtime()
    evidence_id = query(runtime, sql="SELECT SUM(amount) FROM sales")
    denied = runtime.preflight_tool("calculate_from_evidence", {"plan_step_id": "growth"})
    assert not denied["allowed"]
    args = {"plan_step_id": "growth", "evidence_ids": [evidence_id]}
    assert runtime.before_tool("calculate_from_evidence", args)["allowed"]
    runtime.after_tool("calculate_from_evidence", args, '{"mean":15}')
    assert runtime.plan.steps[2].output["evidence_ids"] == [evidence_id]
    runtime.invalidate_step("sales")
    assert not runtime.preflight_tool("calculate_from_evidence", args)["allowed"]


def test_preflight_repair_history_records_actual_corrected_arguments():
    runtime = contracted_runtime()
    bad = {"sql": "SELECT AVG(amount) FROM sales", "plan_step_id": "sales"}
    assert not runtime.preflight_tool("query_mysql", bad)["allowed"]
    assert runtime.request_repair("sales", bad, "contract mismatch")
    query(runtime, sql="SELECT SUM(amount) FROM sales")
    repair = runtime.plan.repair_history[0]
    assert repair["before"]["sql"] == bad["sql"]
    assert repair["after"]["sql"] == "SELECT SUM(amount) FROM sales"
    assert repair["changed_fields"]["sql"]["after"] == repair["after"]["sql"]


def test_file_evidence_accepts_tool_appended_limit_and_tracks_file():
    runtime = contracted_runtime("df", "approved.csv")
    sql = "SELECT SUM(amount) FROM df"
    args = {"plan_step_id": "sales", "file": "approved.csv", "sql": sql}
    assert runtime.before_tool("query_file", args)["allowed"]
    runtime.after_tool("query_file", args, "<!--TABLE:" + json.dumps({
        "columns": ["total"], "rows": [[30]], "executed_sql": sql + " LIMIT 500",
        "source": "df", "truncated": False, "row_count_total": 1,
    }) + "-->")
    assert runtime.validate_evidence()["status"] == "approved"
    assert runtime.plan.evidence[0]["source_name"] == "approved.csv"


def test_single_evidence_reference_and_json_error_status():
    runtime = contracted_runtime()
    evidence_id = query(runtime, sql="SELECT SUM(amount) FROM sales")
    args = {"plan_step_id": "growth", "evidence_id": evidence_id}
    assert runtime.before_tool("calculate_from_evidence", args)["allowed"]
    runtime.after_tool("calculate_from_evidence", args, '{"status":"error","message":"invalid metric"}')
    assert runtime.plan.steps[2].status == "failed"
    assert runtime.plan.steps[2].evidence_ids == [evidence_id]
    assert runtime.validate_evidence()["status"] == "rejected"


@pytest.mark.parametrize("status", ["timeout", "busy", "error"])
def test_global_worker_failure_cannot_be_cleared_by_late_success(status):
    runtime = plan_runtime()
    runtime.before_tool("query_mysql", {"plan_step_id": "sales", "sql": "SELECT amount FROM sales"})
    runtime.record_worker_failure("data_executor", status, "worker incomplete")
    runtime.after_tool("query_mysql", {}, '<!--TABLE:{"columns":["amount"],"rows":[[30]]}-->')
    assert runtime.validate_evidence()["status"] == "rejected"
    assert not runtime.plan.evidence
    assert runtime.next_action() == "stop"


def test_contract_file_stats_does_not_complete_query_step():
    runtime = contracted_runtime()
    assert runtime.before_tool("file_stats", {"file": "approved.csv"})["allowed"]
    runtime.after_tool("file_stats", {"file": "approved.csv"}, "count=100")
    assert runtime.plan.steps[0].status == "pending"
    assert runtime.preflight_tool("query_mysql", {"sql": "SELECT SUM(amount) FROM sales"})["allowed"]


def test_completed_query_cannot_run_as_unplanned_new_evidence():
    runtime = contracted_runtime()
    runtime.plan.steps[1].status = "pending"
    query(runtime, sql="SELECT SUM(amount) FROM sales")
    query(runtime, "cost", sql="SELECT SUM(amount) FROM sales")
    decision = runtime.preflight_tool("query_mysql", {"sql": "SELECT SUM(amount) FROM sales"})
    assert not decision["allowed"] and decision["action"] == "clarify"
    assert any(item["code"] == "unplanned_query_step" for item in decision["issues"])


def test_pending_query_cannot_be_satisfied_by_validation():
    runtime = contracted_runtime()
    runtime.plan.steps[1].status = "pending"
    from app.agent.analysis_plan import PlanStep
    runtime.plan.steps.append(PlanStep(id="v", title="验证", kind="validate", depends_on=["cost"]))
    query(runtime, sql="SELECT SUM(amount) FROM sales")
    assert runtime.validate_evidence()["status"] == "rejected"
    assert runtime.plan.steps[-1].status == "pending"
    assert runtime.plan.steps[1].status == "pending"


def test_consumed_evidence_lineage_is_invalidated_even_without_planner_edge():
    runtime = plan_runtime()
    evidence_id = query(runtime)
    runtime.plan.steps[2].depends_on = []
    runtime.before_tool("calculate_from_evidence", {"plan_step_id": "growth", "evidence_id": evidence_id})
    runtime.after_tool("calculate_from_evidence", {"evidence_id": evidence_id}, '{"value":30}')
    assert runtime.invalidate_step("sales") == ["chart", "growth", "sales"]


def test_explicit_step_id_cannot_complete_query_using_statistical_tool():
    runtime = contracted_runtime()
    args = {"plan_step_id": "sales", "evidence_id": "made_up"}
    result = runtime.preflight_tool("calculate_from_evidence", args)
    assert not result["allowed"] and result["action"] == "clarify"
    assert any(item["code"] == "step_kind_mismatch" for item in result["issues"])


def test_final_completion_gate_requires_calculation_without_blocking_query_prevalidation():
    runtime = contracted_runtime()
    runtime.plan.steps[1].output = {"skip_approved": True, "skip_reason": "independent cost branch excluded"}
    runtime.plan.steps[3].output = {"skip_approved": True, "skip_reason": "user requested a table"}
    runtime.plan.steps[3].status = "skipped"
    evidence_id = query(runtime, sql="SELECT SUM(amount) FROM sales")
    assert runtime.validate_evidence()["status"] == "approved"
    final_report = runtime.validate_evidence(final=True)
    assert final_report["status"] == "rejected" and final_report["should_retry"]
    issue = next(item for item in runtime.plan.active_issues if item["code"] == "missing_required_step")
    assert issue["step_id"] == "growth" and issue["actual"] == "pending"
    assert runtime.validate_evidence()["status"] == "approved"
    assert runtime.request_repair("growth", reason="complete_required_step")
    args = {"plan_step_id": "growth", "evidence_id": evidence_id}
    assert runtime.before_tool("calculate_from_evidence", args)["allowed"]
    runtime.after_tool("calculate_from_evidence", args, '{"result":30}')
    # Repair invalidates the dependent chart; server explicitly confirms its skip.
    runtime.plan.steps[3].status = "skipped"
    runtime.plan.steps[3].output = {"skip_approved": True, "skip_reason": "user requested a table"}
    assert runtime.validate_evidence(final=True)["status"] == "approved"
    assert not runtime.plan.active_issues
    assert any(item["code"] == "missing_required_step" for item in runtime.plan.issue_history)


def test_default_validation_ignores_only_completion_reminders_not_evidence_quality():
    runtime = contracted_runtime()
    query(runtime, rows=[], sql="SELECT SUM(amount) FROM sales")
    runtime.validate_evidence(final=True)
    assert any(item["code"] == "missing_required_step" for item in runtime.plan.active_issues)
    report = runtime.validate_evidence()
    assert report["status"] == "rejected"
    assert "查询返回空结果" in report["issues"]


def test_skipped_required_step_requires_explicit_owner_approval_and_reason():
    runtime = contracted_runtime()
    evidence_id = query(runtime, sql="SELECT SUM(amount) FROM sales")
    for step in runtime.plan.steps[1:]:
        step.status = "skipped"
    report = runtime.validate_evidence(final=True)
    assert report["status"] == "rejected"
    assert any(item["actual"] == "skipped" for item in report["structured_issues"]
               if item["code"] == "missing_required_step")
    assert runtime.get_evidence(evidence_id)["active"]
