"""A missing bar chart can be delivered from approved data without another model call."""

import pytest

from app.agent.analysis_plan import AnalysisPlan, PlanRuntime
from app.api.chat_api import _display_result


def runtime_for(question, rows):
    plan = AnalysisPlan(goal=question, metrics=["销售额", "销售数量"],
                       result_columns=["品类", "销售额", "销售数量"])
    runtime = PlanRuntime(plan, question)
    runtime.after_tool("query_mysql", {"sql": "SELECT category, SUM(amount) FROM sales GROUP BY category"},
        '<!--TABLE:{"columns":["品类","销售额","销售数量"],"rows":' + rows + '}-->')
    runtime.validate_evidence()
    return runtime


def test_deliver_missing_requested_bar_chart_from_verified_table():
    runtime = runtime_for("按品类汇总并画销售额柱状图", '[["甲",15,2],["乙",12,4]]')
    reply, visuals, tables, _ = _display_result([], "统计校验未能通过，暂不绘图。", runtime)
    assert tables[0]["rows"] == [["甲", 15, 2], ["乙", 12, 4]]
    chart = visuals[0]["chart"]
    assert chart["xAxis"]["data"] == ["甲", "乙"]
    assert chart["series"] == [{"type": "bar", "name": "销售额", "data": [15, 12]}]
    assert "通过验证" in reply


@pytest.mark.parametrize("question,rows", [
    ("按品类汇总并画柱状图", '[["甲",15,2],["乙",12,4]]'),
    ("按品类汇总并画销售额柱状图", '[["甲",15,2],["甲",12,4]]'),
    ("按品类汇总并画销售额柱状图", '[]'),
])
def test_no_bar_chart_if_metric_labels_or_evidence_are_missing(question, rows):
    runtime = runtime_for(question, rows)
    _, visuals, _, _ = _display_result([], "统计结果", runtime)
    assert not visuals
