"""可复核的统计分析工具。

LLM 只负责选择方法和传递参数；数值计算在这里由 pandas/numpy 完成。
所有工具返回统一结构：参数、样本量、计算中间结果和最终结果，方便验证和追溯。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from langchain_core.tools import tool


_AGGREGATIONS = {"sum", "mean", "count", "median", "min", "max"}
_PERIOD_FREQS = {"D", "W", "M", "Q", "Y"}


def _records(data: List[Dict[str, Any]]) -> pd.DataFrame:
    if not isinstance(data, list) or not data:
        raise ValueError("data 必须是非空的记录列表")
    frame = pd.DataFrame(data)
    if frame.empty or len(frame.columns) == 0:
        raise ValueError("data 为空，无法计算")
    return frame


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        raise ValueError(f"找不到数值字段：{column}")
    values = pd.to_numeric(frame[column], errors="coerce")
    if int(values.notna().sum()) == 0:
        raise ValueError(f"字段「{column}」没有可计算的数值")
    return values


def _aggregation(values: pd.Series, aggregation: str) -> float:
    method = (aggregation or "sum").lower()
    if method not in _AGGREGATIONS:
        raise ValueError(f"不支持的聚合方式：{aggregation}，可选：{sorted(_AGGREGATIONS)}")
    if method == "count":
        return int(values.count())
    return float(getattr(values, method)())


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (pd.Period, pd.Timestamp)):
        return str(value)
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return _json_value(value)


def _envelope(operation: str, parameters: Dict[str, Any], sample_size: int,
              result: Dict[str, Any], intermediate: Dict[str, Any]) -> Dict[str, Any]:
    return _clean({
        "status": "ok",
        "operation": operation,
        "parameters": parameters,
        "sample_size": int(sample_size),
        "intermediate": intermediate,
        "result": result,
    })


def _error(operation: str, exc: Exception) -> str:
    return json.dumps({"status": "error", "operation": operation, "message": str(exc)},
                      ensure_ascii=False)


def _periods(frame: pd.DataFrame, period_col: str, value_col: str, freq: str = "M") -> Tuple[pd.DataFrame, int]:
    if period_col not in frame.columns:
        raise ValueError(f"找不到时间字段：{period_col}")
    freq = (freq or "M").upper()
    if freq not in _PERIOD_FREQS:
        raise ValueError(f"不支持的时间粒度：{freq}，可选：{sorted(_PERIOD_FREQS)}")
    values = _numeric(frame, value_col)
    dates = pd.to_datetime(frame[period_col], errors="coerce")
    valid = dates.notna() & values.notna()
    if int(valid.sum()) == 0:
        raise ValueError("时间字段或数值字段没有可计算的有效记录")
    clean = frame.loc[valid].copy()
    clean["__value"] = values.loc[valid].astype(float)
    clean["__period"] = dates.loc[valid].dt.to_period(freq)
    return clean, int(valid.sum())


def compare_periods(data: List[Dict[str, Any]], period_col: str, value_col: str,
                    comparison: str = "yoy", aggregation: str = "sum",
                    group_by: Optional[List[str]] = None, period_freq: str = "M") -> Dict[str, Any]:
    """计算同比或环比；comparison 取 yoy 或 mom，默认按月聚合。"""
    comparison = (comparison or "yoy").lower()
    if comparison not in {"yoy", "mom"}:
        raise ValueError("comparison 只能是 yoy 或 mom")
    group_by = group_by or []
    frame = _records(data)
    missing = [column for column in group_by if column not in frame.columns]
    if missing:
        raise ValueError(f"找不到分组字段：{missing}")
    clean, sample_size = _periods(frame, period_col, value_col, period_freq)
    keys = ["__period"] + group_by
    grouped = clean.groupby(keys, dropna=False, sort=True)["__value"].agg(aggregation).reset_index()
    offset = 12 if comparison == "yoy" and period_freq.upper() == "M" else 1
    rows: List[Dict[str, Any]] = []
    for group_values, subset in grouped.groupby(group_by, dropna=False, sort=True) if group_by else [((), grouped)]:
        if not isinstance(group_values, tuple):
            group_values = (group_values,)
        lookup = {row["__period"]: float(row["__value"]) for row in subset.to_dict("records")}
        for current in sorted(lookup):
            previous = current - offset
            if previous not in lookup:
                continue
            old = lookup[previous]
            change = lookup[current] - old
            row = {
                "current_period": str(current),
                "previous_period": str(previous),
                "current_value": lookup[current],
                "previous_value": old,
                "change": change,
                "change_rate_pct": None if old == 0 else change / old * 100,
            }
            row.update({column: _json_value(value) for column, value in zip(group_by, group_values)})
            rows.append(row)
    aggregated = [{str(k): _json_value(v) for k, v in row.items() if k != "__period"} for row in grouped.to_dict("records")]
    return _envelope(
        "period_comparison",
        {"period_col": period_col, "value_col": value_col, "comparison": comparison,
         "aggregation": aggregation, "group_by": group_by, "period_freq": period_freq.upper()},
        sample_size,
        {"comparisons": rows, "matched_periods": len(rows)},
        {"aggregated_periods": aggregated, "offset_periods": offset},
    )


def compare_groups(data: List[Dict[str, Any]], group_by: List[str], value_col: str,
                   aggregation: str = "sum") -> Dict[str, Any]:
    """按一个或多个维度聚合并返回分组之间的差异。"""
    if not group_by:
        raise ValueError("group_by 至少需要一个字段")
    frame = _records(data)
    missing = [column for column in group_by if column not in frame.columns]
    if missing:
        raise ValueError(f"找不到分组字段：{missing}")
    values = _numeric(frame, value_col)
    valid = values.notna()
    clean = frame.loc[valid, group_by].copy()
    clean["__value"] = values.loc[valid].astype(float)
    grouped = clean.groupby(group_by, dropna=False, sort=True)["__value"].agg(aggregation).reset_index()
    rows = [{str(k): _json_value(v) for k, v in row.items()} for row in grouped.to_dict("records")]
    numbers = [float(row["__value"]) for row in grouped.to_dict("records")]
    maximum = max(numbers) if numbers else None
    minimum = min(numbers) if numbers else None
    return _envelope(
        "group_comparison",
        {"group_by": group_by, "value_col": value_col, "aggregation": aggregation},
        int(valid.sum()),
        {"groups": [{k: v for k, v in row.items() if k != "__value"} | {"value": row["__value"]} for row in rows],
         "max_value": maximum, "min_value": minimum,
         "difference": None if maximum is None or minimum is None else maximum - minimum},
        {"group_count": len(rows), "valid_rows": int(valid.sum())},
    )


def analyze_trend(data: List[Dict[str, Any]], period_col: str, value_col: str,
                  aggregation: str = "sum", period_freq: str = "M") -> Dict[str, Any]:
    """按时间聚合并用最小二乘直线计算趋势方向和斜率。"""
    frame = _records(data)
    clean, sample_size = _periods(frame, period_col, value_col, period_freq)
    grouped = clean.groupby("__period", sort=True)["__value"].agg(aggregation).reset_index()
    values = grouped["__value"].astype(float).to_numpy()
    slope = float(np.polyfit(np.arange(len(values)), values, 1)[0]) if len(values) >= 2 else 0.0
    first = float(values[0])
    last = float(values[-1])
    change = last - first
    points = [{"period": str(row["__period"]), "value": float(row["__value"])}
              for row in grouped.to_dict("records")]
    return _envelope(
        "trend",
        {"period_col": period_col, "value_col": value_col, "aggregation": aggregation,
         "period_freq": period_freq.upper()},
        sample_size,
        {"points": points, "direction": "up" if slope > 0 else "down" if slope < 0 else "flat",
         "slope": slope, "first_value": first, "last_value": last, "change": change,
         "change_rate_pct": None if first == 0 else change / first * 100},
        {"period_count": len(points), "regression_method": "ordinary_least_squares", "x_axis": "按时间排序后的序号"},
    )


def detect_outliers(data: List[Dict[str, Any]], value_col: str, method: str = "iqr",
                    threshold: float = 1.5) -> Dict[str, Any]:
    """使用 IQR 或 z-score 检测异常值，不改变原始数据。"""
    frame = _records(data)
    values = _numeric(frame, value_col)
    valid = values.notna()
    series = values.loc[valid].astype(float)
    method = (method or "iqr").lower()
    if method == "iqr":
        q1 = float(series.quantile(0.25))
        q3 = float(series.quantile(0.75))
        spread = q3 - q1
        lower, upper = q1 - float(threshold) * spread, q3 + float(threshold) * spread
        intermediate = {"q1": q1, "q3": q3, "iqr": spread}
    elif method == "zscore":
        mean = float(series.mean())
        std = float(series.std(ddof=0))
        lower, upper = mean - float(threshold) * std, mean + float(threshold) * std
        intermediate = {"mean": mean, "std": std}
    else:
        raise ValueError("method 只能是 iqr 或 zscore")
    mask = (series < lower) | (series > upper)
    outliers = [{"row_index": _json_value(index), "value": float(value)}
                for index, value in series.loc[mask].items()]
    return _envelope(
        "outlier_detection",
        {"value_col": value_col, "method": method, "threshold": float(threshold)},
        int(valid.sum()),
        {"outlier_count": len(outliers), "outliers": outliers,
         "lower_bound": lower, "upper_bound": upper},
        intermediate | {"valid_rows": int(valid.sum())},
    )


def analyze_correlation(data: List[Dict[str, Any]], value_cols: Optional[List[str]] = None) -> Dict[str, Any]:
    """计算 Pearson 相关系数矩阵，并返回有效样本量和最强相关对。"""
    frame = _records(data)
    if value_cols:
        missing = [column for column in value_cols if column not in frame.columns]
        if missing:
            raise ValueError(f"找不到字段：{missing}")
        columns = value_cols
    else:
        columns = frame.select_dtypes(include=[np.number]).columns.tolist()
    if len(columns) < 2:
        raise ValueError("相关性分析至少需要两个数值字段")
    numeric = frame[columns].apply(pd.to_numeric, errors="coerce")
    corr = numeric.corr(method="pearson")
    pairs: List[Dict[str, Any]] = []
    for index, left in enumerate(columns):
        for right in columns[index + 1:]:
            pair = numeric[[left, right]].dropna()
            coefficient = corr.loc[left, right]
            pairs.append({"left": left, "right": right, "correlation": _json_value(coefficient),
                          "sample_size": len(pair)})
    pairs.sort(key=lambda row: abs(row["correlation"] or 0), reverse=True)
    matrix = {column: {other: _json_value(corr.loc[column, other]) for other in columns} for column in columns}
    return _envelope(
        "correlation",
        {"value_cols": columns, "method": "pearson"},
        len(numeric.dropna(how="any")),
        {"matrix": matrix, "pairs": pairs, "strongest_pair": pairs[0] if pairs else None},
        {"column_count": len(columns), "pairwise_sample_sizes": {f"{row['left']}~{row['right']}": row["sample_size"] for row in pairs}},
    )


def _as_json(operation: str, fn, *args, **kwargs) -> str:
    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False, default=str)
    except (TypeError, ValueError, KeyError) as exc:
        return _error(operation, exc)


def make_statistical_tools() -> list:
    """构造给 statistical_validator 子图使用的 LangChain 工具。"""
    @tool
    def compare_periods_tool(data: List[Dict[str, Any]], period_col: str, value_col: str,
                             comparison: str = "yoy", aggregation: str = "sum",
                             group_by: Optional[List[str]] = None, period_freq: str = "M") -> str:
        """对真实查询记录计算同比或环比。data 使用记录列表；不要手工计算 change_rate。"""
        return _as_json("period_comparison", compare_periods, data, period_col, value_col,
                        comparison, aggregation, group_by, period_freq)

    @tool
    def compare_groups_tool(data: List[Dict[str, Any]], group_by: List[str], value_col: str,
                            aggregation: str = "sum") -> str:
        """对真实查询记录做分组聚合和组间差异比较。"""
        return _as_json("group_comparison", compare_groups, data, group_by, value_col, aggregation)

    @tool
    def analyze_trend_tool(data: List[Dict[str, Any]], period_col: str, value_col: str,
                           aggregation: str = "sum", period_freq: str = "M") -> str:
        """对真实查询记录按时间聚合并计算趋势斜率。"""
        return _as_json("trend", analyze_trend, data, period_col, value_col, aggregation, period_freq)

    @tool
    def detect_outliers_tool(data: List[Dict[str, Any]], value_col: str, method: str = "iqr",
                             threshold: float = 1.5) -> str:
        """对真实查询记录检测异常值；默认使用 IQR，不删除异常记录。"""
        return _as_json("outlier_detection", detect_outliers, data, value_col, method, threshold)

    @tool
    def analyze_correlation_tool(data: List[Dict[str, Any]], value_cols: Optional[List[str]] = None) -> str:
        """对真实查询记录计算 Pearson 相关系数矩阵和有效样本量。"""
        return _as_json("correlation", analyze_correlation, data, value_cols)

    return [compare_periods_tool, compare_groups_tool, analyze_trend_tool,
            detect_outliers_tool, analyze_correlation_tool]
