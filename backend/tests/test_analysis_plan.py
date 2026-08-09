"""分析计划 StateGraph 单测。"""
from langchain_core.messages import AIMessage

from app.agent.analysis_plan import AnalysisPlan, PlanRuntime, make_analysis_plan_graph, plan_context


class FakePlanner:
    def __init__(self, content):
        self.content = content

    def invoke(self, _messages):
        return AIMessage(content=self.content)


def test_plan_graph_parses_and_cleans_dependencies():
    llm = FakePlanner('''{"goal":"统计订单","data_sources":["share-order.orders"],
      "metrics":["订单数"],"dimensions":["地区"],"filters":[],
      "steps":[{"id":"query","title":"查询","kind":"query","depends_on":["query","missing"]}],
      "output_format":"table","assumptions":[],"clarification_needed":false}''')
    result = make_analysis_plan_graph(llm).invoke({
        "question": "统计订单", "catalog": [{"name": "share-order.orders"}],
        "plan": None, "error": None,
    })
    plan = result["plan"]
    assert isinstance(plan, AnalysisPlan)
    assert plan.steps[0].depends_on == []
    assert plan.output_format == "table"
    assert "统计订单" in plan_context(plan)


def test_plan_graph_falls_back_on_invalid_json():
    result = make_analysis_plan_graph(FakePlanner("不是 JSON")).invoke({
        "question": "查订单", "catalog": [{"name": "share-order.orders"}],
        "plan": None, "error": None,
    })
    plan = result["plan"]
    assert plan.error
    assert [step.id for step in plan.steps] == ["query_1", "validate_1", "summarize_1"]


def test_plan_runtime_records_evidence_and_quality_issue():
    plan = AnalysisPlan(
        goal="对比订单", steps=[
            {"id": "query", "title": "查询", "kind": "query"},
            {"id": "validate", "title": "校验", "kind": "validate"},
        ]
    )
    runtime = PlanRuntime(plan, "对比订单")
    runtime.before_tool("query_mysql", {"sql": "SELECT 1"})
    issues = runtime.after_tool(
        "query_mysql", {"sql": "SELECT 1"},
        '| a |\n| --- |\n| 1 |\n<!--TABLE:{"columns":["a"],"rows":[[1]]}-->'
    )
    runtime.finalize()
    assert "样本量不足，无法支持比较类结论" in issues
    assert plan.evidence[0]["row_count"] == 1
    assert plan.steps[0].status == "succeeded"
    assert plan.steps[1].status == "failed"


def test_plan_runtime_validation_gate_requires_clean_evidence():
    plan = AnalysisPlan(
        goal="统计订单", metrics=["订单数"], steps=[
            {"id": "query", "title": "查询", "kind": "query"},
            {"id": "validate", "title": "验证", "kind": "validate"},
        ]
    )
    runtime = PlanRuntime(plan, "统计订单")
    rejected = runtime.validate_evidence()
    assert rejected["status"] == "rejected"
    assert "没有真实查询证据" in rejected["issues"]
    assert runtime.validation_status == "rejected"

    plan.evidence.append({"source": "query_mysql", "row_count": 1, "quality_issues": []})
    runtime.validation_status = "pending"
    approved = runtime.validate_evidence()
    assert approved["status"] == "approved"
    assert plan.steps[1].status == "succeeded"


def test_plan_runtime_rejects_failed_query_even_without_quality_issue():
    plan = AnalysisPlan(
        goal="统计订单", metrics=["订单数"], steps=[
            {"id": "query", "title": "查询", "kind": "query"},
            {"id": "validate", "title": "验证", "kind": "validate"},
        ]
    )
    runtime = PlanRuntime(plan, "统计订单")
    runtime.before_tool("query_mysql", {"sql": "SELECT broken"})
    runtime.after_tool("query_mysql", {"sql": "SELECT broken"}, "SQL 执行错误：字段不存在")
    report = runtime.validate_evidence()
    assert report["status"] == "rejected"
    assert "数据执行步骤失败" in report["issues"]
