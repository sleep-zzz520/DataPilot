"""统一统计工具的纯函数与 LangChain 包装测试。"""
import json

import pytest

from app.tools.statistical_tool import (
    analyze_correlation,
    analyze_trend,
    compare_groups,
    compare_periods,
    detect_outliers,
    make_statistical_tools,
)


def _monthly_rows():
    return [
        {"month": "2024-01-01", "city": "北京", "sales": 80},
        {"month": "2024-02-01", "city": "北京", "sales": 100},
        {"month": "2025-01-01", "city": "北京", "sales": 100},
        {"month": "2025-02-01", "city": "北京", "sales": 120},
        {"month": "2025-01-01", "city": "上海", "sales": 50},
        {"month": "2025-02-01", "city": "上海", "sales": 70},
    ]


def test_compare_periods_returns_yoy_and_reproducible_parameters():
    out = compare_periods(_monthly_rows(), "month", "sales", "yoy", "sum", ["city"], "M")
    assert out["status"] == "ok"
    assert out["sample_size"] == 6
    assert out["parameters"]["comparison"] == "yoy"
    beijing = next(row for row in out["result"]["comparisons"]
                   if row["current_period"] == "2025-01" and row["city"] == "北京")
    assert beijing["previous_value"] == 80
    assert beijing["change_rate_pct"] == pytest.approx(25.0)


def test_compare_periods_mom_supports_grouping():
    out = compare_periods(_monthly_rows(), "month", "sales", "mom", "sum", ["city"], "M")
    shanghai = next(row for row in out["result"]["comparisons"] if row["city"] == "上海")
    assert shanghai["previous_period"] == "2025-01"
    assert shanghai["change"] == 20


def test_group_compare_returns_sample_and_difference():
    out = compare_groups(_monthly_rows(), ["city"], "sales", "mean")
    assert out["sample_size"] == 6
    assert {row["city"] for row in out["result"]["groups"]} == {"北京", "上海"}
    assert out["result"]["difference"] == pytest.approx(40.0)


def test_trend_uses_sorted_periods_and_reports_slope():
    out = analyze_trend([
        {"month": "2025-03-01", "sales": 30},
        {"month": "2025-01-01", "sales": 10},
        {"month": "2025-02-01", "sales": 20},
    ], "month", "sales")
    assert out["result"]["direction"] == "up"
    assert out["result"]["slope"] == pytest.approx(10.0)
    assert [row["period"] for row in out["result"]["points"]] == ["2025-01", "2025-02", "2025-03"]


def test_outlier_detection_returns_iqr_bounds_without_deleting_data():
    out = detect_outliers([{"value": value} for value in [1, 2, 3, 100]], "value")
    assert out["sample_size"] == 4
    assert out["result"]["outlier_count"] == 1
    assert out["result"]["outliers"][0]["value"] == 100


def test_correlation_returns_pairwise_sample_size_and_matrix():
    out = analyze_correlation([
        {"x": 1, "y": 2, "z": 5},
        {"x": 2, "y": 4, "z": 4},
        {"x": 3, "y": 6, "z": 3},
    ])
    assert out["sample_size"] == 3
    assert out["result"]["matrix"]["x"]["y"] == pytest.approx(1.0)
    assert out["result"]["strongest_pair"]["sample_size"] == 3


def test_statistical_tools_return_json_and_reject_bad_columns():
    tools = {item.name: item for item in make_statistical_tools()}
    out = json.loads(tools["compare_groups_tool"].invoke({
        "data": [{"group": "A", "value": 1}], "group_by": ["missing"], "value_col": "value",
    }))
    assert out["status"] == "error"
    assert "missing" in out["message"]


def test_tool_schemas_resolve_without_python310_union_syntax():
    """LangChain/Pydantic 在 CI 的 Python 3.9 下也能构造参数 schema。"""
    tools = make_statistical_tools()
    assert len(tools) == 5
    assert all(tool.args_schema is not None for tool in tools)
