"""图表辅助逻辑（tools/chart_tool.py 的纯函数）单测：数据校验与图表选型。"""
import pandas as pd
import pytest

from app.core.process_executor import LocalToolTimeoutError
from app.tools import chart_tool
from app.tools.chart_tool import _validate_data, _auto_select_chart_type, generate_chart


# ── 数据校验 ──────────────────────────────────────────────────────────────────
def test_validate_ok_numeric_column():
    df = _validate_data([{"城市": "北京", "销量": 100}])
    assert not df.empty


def test_validate_empty_raises():
    with pytest.raises(ValueError, match="数据为空"):
        _validate_data([])


def test_validate_no_numeric_and_single_category_raises():
    with pytest.raises(ValueError, match="至少需要"):
        _validate_data([{"a": "x"}, {"a": "y"}])


def test_validate_two_categories_ok():
    df = _validate_data([{"a": "x", "b": "y"}])
    assert not df.empty


# ── 图表选型 ──────────────────────────────────────────────────────────────────
def test_auto_select_passthrough_intent():
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    assert _auto_select_chart_type(df, "trend") == "trend"
    assert _auto_select_chart_type(df, "relationship") == "relationship"


def test_auto_select_two_numeric_small_scatter():
    df = pd.DataFrame({"x": range(20), "y": range(20)})
    assert _auto_select_chart_type(df, "auto") == "scatter"


def test_auto_select_two_numeric_large_heatmap():
    df = pd.DataFrame({"x": range(200), "y": range(200)})
    assert _auto_select_chart_type(df, "auto") == "heatmap"


def test_auto_select_category_plus_numeric_many_categories_bar():
    df = pd.DataFrame({"cat": [f"c{i}" for i in range(15)], "val": range(15)})
    assert _auto_select_chart_type(df, "auto") == "bar"


def test_auto_select_category_plus_numeric_few_pie():
    df = pd.DataFrame({"cat": ["A", "B", "C"], "val": [1, 2, 3]})
    assert _auto_select_chart_type(df, "auto") == "pie"


def test_auto_select_single_numeric_histogram():
    df = pd.DataFrame({"val": [1, 2, 3]})
    assert _auto_select_chart_type(df, "auto") == "histogram"


def test_generate_chart_runs_in_isolated_process():
    out = generate_chart.invoke({
        "chart_type": "bar",
        "title": "城市销量",
        "data": [{"城市": "北京", "销量": 1}, {"城市": "上海", "销量": 2}],
    })
    assert "<!--CHART:" in out and "<!--IMAGE_BASE64:" in out


def test_generate_chart_process_timeout_is_machine_readable(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise LocalToolTimeoutError("too slow")

    monkeypatch.setattr(chart_tool, "run_in_process", timeout)
    out = generate_chart.invoke({
        "chart_type": "bar",
        "title": "城市销量",
        "data": [{"城市": "北京", "销量": 1}],
    })
    assert out.startswith("[TOOL_TIMEOUT] generate_chart")
