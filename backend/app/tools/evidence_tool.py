"""Statistics and charts consume controlled evidence references, never copied rows."""
import json
from langchain_core.tools import tool


def make_evidence_tools(runtime, tools):
    by_name = {item.name: item for item in tools}
    operations = {
        "compare_periods": "compare_periods_tool", "compare_groups": "compare_groups_tool",
        "trend": "analyze_trend_tool", "outliers": "detect_outliers_tool",
        "correlation": "analyze_correlation_tool",
    }

    @tool
    def calculate_from_evidence(evidence_id: str, operation: str, parameters: dict) -> str:
        """Use an active evidence ID for statistics; parameters excludes data/records.

        operation: compare_periods/compare_groups/trend/outliers/correlation.
        All numeric input is loaded from the server evidence store.
        """
        try:
            if any(key in parameters for key in ("data", "records", "evidence_id", "evidence_ids")):
                raise ValueError("参数不能替换受控证据数据")
            fn = by_name.get(operations.get(operation, ""))
            if fn is None:
                raise ValueError("不支持的统计操作")
            payload = runtime.evidence_rows([evidence_id])
            records = [dict(zip(payload["columns"], row)) for row in payload["rows"]]
            result = fn.invoke({**parameters, "data": records})
            return json.dumps({"evidence_ids": [evidence_id], "operation": operation,
                               "result": str(result)}, ensure_ascii=False)
        except (ValueError, KeyError, TypeError) as exc:
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)

    @tool
    def chart_from_evidence(evidence_id: str, chart_type: str, x_column: str,
                            y_columns: list[str], title: str) -> str:
        """Create bar/line ECharts directly from an approved evidence ID and column names."""
        try:
            if runtime.validate_evidence()["status"] != "approved":
                raise ValueError("证据尚未通过验证")
            if chart_type not in {"bar", "line"}:
                raise ValueError("引用图表仅支持bar/line")
            payload = runtime.evidence_rows([evidence_id])
            columns, rows = payload["columns"], payload["rows"]
            if not y_columns or len(set(y_columns)) != len(y_columns):
                raise ValueError("必须提供不重复的数值列")
            positions = [columns.index(name) for name in y_columns]
            x_index = columns.index(x_column)
            series = [{"name": name, "data": [row[pos] for row in rows]}
                      for name, pos in zip(y_columns, positions)]
            if any(not isinstance(value, (int, float)) or isinstance(value, bool)
                   for item in series for value in item["data"]):
                raise ValueError("数值列包含非数值")
            fn = by_name.get("make_chart")
            if fn is None:
                raise ValueError("图表工具不可用")
            result = fn.invoke({"chart_type": chart_type, "title": title,
                              "x_labels": [str(row[x_index]) for row in rows], "series": series})
            return str(result) + "\n" + json.dumps({"evidence_ids": [evidence_id]}, ensure_ascii=False)
        except (ValueError, KeyError, TypeError) as exc:
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)

    return [calculate_from_evidence, chart_from_evidence]
