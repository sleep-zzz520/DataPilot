from app.agent.analysis_plan import AnalysisPlan, PlanRuntime
from app.api.chat_api import _display_result


def test_unresolved_tool_timeout_is_reported_after_plan_warning_disappears():
    runtime = PlanRuntime(AnalysisPlan(goal="统计文件", metrics=["总量"]))
    runtime.validation_status = "rejected"
    assert not runtime.plan.quality_issues
    reply, visuals, tables, _ = _display_result([], "共 100", runtime, [{"tool": "query_file", "status": "timeout"}])
    assert "超时" in reply and "100" not in reply
    assert not visuals and not tables
