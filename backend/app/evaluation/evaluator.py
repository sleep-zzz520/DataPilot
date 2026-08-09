"""分析 Agent 的离线评测器。

评测输入不依赖 LLM 或数据库，结构与 chat API 返回的核心字段保持一致：
plan、sql、validation、evidence、trace、reply。真实模型评测只需要把预测结果
替换到同样的 ``prediction`` 字段，不改变打分规则。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_CASES_PATH = Path(__file__).with_name("cases.json")
_WRITE_SQL = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke)\b", re.I)


def load_cases(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取评测集；支持 JSON 数组或 ``{"cases": [...]}`` 格式。"""
    case_path = Path(path) if path else DEFAULT_CASES_PATH
    payload = json.loads(case_path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list) or not cases:
        raise ValueError("评测集必须是非空数组")
    if any(not isinstance(case, dict) for case in cases):
        raise ValueError("评测集中的每个样例必须是对象")
    return cases


def _items(value: Any) -> List[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _canonical(value: Any, aliases: Optional[Dict[str, str]] = None) -> str:
    text = _text(value)
    for source, target in sorted((aliases or {}).items(), key=lambda item: len(_text(item[0])), reverse=True):
        text = text.replace(_text(source), _text(target))
    return text


def _matches(expected: Any, actual: Any, aliases: Optional[Dict[str, str]] = None) -> bool:
    if isinstance(expected, dict) and "any_of" in expected:
        return any(_matches(option, actual, aliases) for option in expected["any_of"])
    expected_text = _canonical(expected, aliases)
    actual_text = _canonical(actual, aliases)
    return bool(expected_text and actual_text) and (expected_text == actual_text or
                                                    expected_text in actual_text or actual_text in expected_text)


def _list_score(expected: Any, actual: Any, aliases: Optional[Dict[str, str]] = None) -> float:
    expected_items = _items(expected)
    actual_items = _items(actual)
    if not expected_items:
        return 1.0 if not actual_items else 0.0
    used = set()
    matched = 0
    for item in expected_items:
        for index, candidate in enumerate(actual_items):
            if index not in used and _matches(item, candidate, aliases):
                used.add(index)
                matched += 1
                break
    precision = matched / len(actual_items) if actual_items else 0.0
    recall = matched / len(expected_items)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _check(name: str, score: float, passed: Optional[bool] = None) -> Dict[str, Any]:
    return {
        "name": name,
        "score": round(float(score), 4),
        "passed": bool(score >= 1.0 if passed is None else passed),
    }


def _plan_result(case: Dict[str, Any]) -> Dict[str, Any]:
    gold = case.get("gold", {}).get("plan", {})
    actual = case.get("prediction", {}).get("plan") or {}
    aliases = case.get("gold", {}).get("aliases", {})
    checks: List[Dict[str, Any]] = []

    if "goal_keywords" in gold:
        keywords = _items(gold["goal_keywords"])
        matched = sum(1 for keyword in keywords if _matches(keyword, actual.get("goal", ""), aliases))
        checks.append(_check("goal_keywords", matched / len(keywords) if keywords else 1.0,
                             matched == len(keywords)))
    for field in ("data_sources", "metrics", "dimensions", "filters"):
        if field in gold:
            score = _list_score(gold[field], actual.get(field), aliases)
            checks.append(_check(field, score))
    for field in ("time_range", "grain", "sort", "unit"):
        if field in gold:
            score = 1.0 if _matches(gold[field], actual.get(field), aliases) else 0.0
            checks.append(_check(field, score))
    if "required_step_kinds" in gold:
        expected_steps = set(_text(item) for item in _items(gold["required_step_kinds"]))
        actual_steps = set(_text(step.get("kind")) for step in _items(actual.get("steps")) if isinstance(step, dict))
        score = len(expected_steps & actual_steps) / len(expected_steps) if expected_steps else 1.0
        checks.append(_check("required_step_kinds", score, expected_steps <= actual_steps))
    if "clarification_needed" in gold:
        expected = bool(gold["clarification_needed"])
        checks.append(_check("clarification_needed", 1.0 if bool(actual.get("clarification_needed")) == expected else 0.0))

    score = sum(item["score"] for item in checks) / len(checks) if checks else 0.0
    return {"score": round(score, 4), "correct": all(item["passed"] for item in checks), "checks": checks}


def _normalize_sql(sql: Any) -> str:
    return re.sub(r"\s+", " ", str(sql or "").strip().lower())


def _sql_result(case: Dict[str, Any]) -> Dict[str, Any]:
    gold = case.get("gold", {}).get("sql", {})
    actual = case.get("prediction", {}).get("sql")
    checks: List[Dict[str, Any]] = []
    if gold.get("not_executed"):
        passed = not _normalize_sql(actual)
        return {"score": 1.0 if passed else 0.0, "correct": passed,
                "checks": [_check("not_executed", 1.0 if passed else 0.0)]}

    sql = _normalize_sql(actual)
    if gold.get("read_only"):
        passed = sql.startswith("select ") or sql == "select"
        passed = passed and not _WRITE_SQL.search(sql)
        checks.append(_check("read_only", 1.0 if passed else 0.0))
    for fragment in gold.get("required_fragments", []):
        passed = _text(fragment) in sql
        checks.append(_check("required:" + str(fragment), 1.0 if passed else 0.0))
    for fragment in gold.get("forbidden_fragments", []):
        passed = _text(fragment) not in sql
        checks.append(_check("forbidden:" + str(fragment), 1.0 if passed else 0.0))
    if "exact" in gold:
        passed = sql == _normalize_sql(gold["exact"])
        checks.append(_check("exact", 1.0 if passed else 0.0))
    score = sum(item["score"] for item in checks) / len(checks) if checks else 0.0
    return {"score": round(score, 4), "correct": all(item["passed"] for item in checks), "checks": checks}


def _issue_matches(expected: Any, issues: Iterable[Any], aliases: Optional[Dict[str, str]] = None) -> bool:
    return any(_matches(expected, issue, aliases) for issue in issues)


def _validation_result(case: Dict[str, Any]) -> Dict[str, Any]:
    gold = case.get("gold", {}).get("validation", {})
    actual = case.get("prediction", {}).get("validation") or {}
    aliases = case.get("gold", {}).get("aliases", {})
    issues = _items(actual.get("issues"))
    checks: List[Dict[str, Any]] = []
    if "status" in gold:
        passed = _text(gold["status"]) == _text(actual.get("status"))
        checks.append(_check("status", 1.0 if passed else 0.0))
    must_detect = _items(gold.get("must_detect"))
    for issue in must_detect:
        passed = _issue_matches(issue, issues, aliases)
        checks.append(_check("must_detect:" + str(issue), 1.0 if passed else 0.0))
    for issue in _items(gold.get("must_not_detect")):
        passed = not _issue_matches(issue, issues, aliases)
        checks.append(_check("must_not_detect:" + str(issue), 1.0 if passed else 0.0))
    if gold.get("must_be_clean"):
        passed = not issues
        checks.append(_check("must_be_clean", 1.0 if passed else 0.0))
    score = sum(item["score"] for item in checks) / len(checks) if checks else 0.0
    detected = sum(1 for issue in must_detect if _issue_matches(issue, issues, aliases))
    return {
        "score": round(score, 4),
        "correct": all(item["passed"] for item in checks),
        "checks": checks,
        "detected": detected,
        "expected_issue_count": len(must_detect),
    }


def _traceability_result(case: Dict[str, Any]) -> Dict[str, Any]:
    gold = case.get("gold", {}).get("traceability", {})
    actual = case.get("prediction", {})
    if gold.get("not_required"):
        return {"score": 1.0, "correct": True, "checks": [_check("not_required", 1.0)]}

    evidence = [item for item in _items(actual.get("evidence")) if isinstance(item, dict)]
    checks: List[Dict[str, Any]] = []
    min_evidence = int(gold.get("min_evidence", 1))
    checks.append(_check("min_evidence", 1.0 if len(evidence) >= min_evidence else 0.0))
    for field in gold.get("required_evidence_fields", []):
        # 空列表（如 quality_issues=[]）表示已完成检查且无问题，仍是有效证据。
        present = bool(evidence) and all(field in item and item.get(field) not in (None, "") for item in evidence)
        checks.append(_check("evidence_field:" + field, 1.0 if present else 0.0))
    reply = _text(actual.get("reply"))
    for keyword in gold.get("reply_keywords", []):
        checks.append(_check("reply_keyword:" + str(keyword),
                             1.0 if _text(keyword) in reply else 0.0))
    traces = [item for item in _items(actual.get("trace")) if isinstance(item, dict)]
    for tool in gold.get("required_trace_tools", []):
        passed = any(_text(item.get("tool")) == _text(tool) for item in traces)
        checks.append(_check("trace_tool:" + str(tool), 1.0 if passed else 0.0))
    score = sum(item["score"] for item in checks) / len(checks) if checks else 0.0
    return {"score": round(score, 4), "correct": all(item["passed"] for item in checks), "checks": checks}


def evaluate_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """评估单个样例，返回可直接序列化的明细。"""
    if not case.get("id"):
        raise ValueError("评测样例缺少 id")
    results = {
        "plan": _plan_result(case),
        "sql": _sql_result(case),
        "validation": _validation_result(case),
        "traceability": _traceability_result(case),
    }
    return {
        "id": case["id"],
        "question": case.get("question", ""),
        "passed": all(item["correct"] for item in results.values()),
        "metrics": results,
    }


def _rate(results: List[Dict[str, Any]], name: str) -> float:
    if not results:
        return 0.0
    return sum(1 for result in results if result["metrics"][name]["correct"]) / len(results)


def evaluate_dataset(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """批量评估并汇总四项核心指标。"""
    results = [evaluate_case(case) for case in cases]
    metrics: Dict[str, Dict[str, Any]] = {}
    metric_names = {
        "plan": "plan_accuracy",
        "sql": "sql_accuracy",
        "validation": "result_validation_rate",
        "traceability": "conclusion_traceability_rate",
    }
    for internal_name, public_name in metric_names.items():
        rate = _rate(results, internal_name)
        scores = [result["metrics"][internal_name]["score"] for result in results]
        metrics[public_name] = {
            "rate": round(rate, 4),
            "correct": sum(1 for result in results if result["metrics"][internal_name]["correct"]),
            "total": len(results),
            "average_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        }
    expected_issues = sum(result["metrics"]["validation"]["expected_issue_count"] for result in results)
    detected_issues = sum(result["metrics"]["validation"]["detected"] for result in results)
    metrics["result_validation_rate"]["recall"] = round(
        detected_issues / expected_issues if expected_issues else 1.0, 4,
    )
    overall = sum(item["rate"] for item in metrics.values()) / 4 if metrics else 0.0
    return {
        "case_count": len(results),
        "metrics": metrics,
        "overall_rate": round(overall, 4),
        "all_cases_passed": all(result["passed"] for result in results),
        "cases": results,
    }
