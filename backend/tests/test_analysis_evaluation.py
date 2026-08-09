"""离线分析评测集与核心打分规则。"""
from copy import deepcopy

from app.evaluation import evaluate_case, evaluate_dataset, load_cases


def _case(case_id):
    return next(case for case in load_cases() if case["id"] == case_id)


def test_builtin_evaluation_set_covers_all_metrics():
    report = evaluate_dataset(load_cases())
    assert report["case_count"] == 7
    assert report["all_cases_passed"] is True
    assert report["metrics"]["plan_accuracy"]["rate"] == 1.0
    assert report["metrics"]["sql_accuracy"]["rate"] == 1.0
    assert report["metrics"]["result_validation_rate"]["rate"] == 1.0
    assert report["metrics"]["result_validation_rate"]["recall"] == 1.0
    assert report["metrics"]["conclusion_traceability_rate"]["rate"] == 1.0


def test_wrong_field_is_rejected_by_sql_metric():
    case = deepcopy(_case("wrong_field_guard"))
    case["prediction"]["sql"] = case["prediction"]["sql"].replace("order_date", "created_at")
    result = evaluate_case(case)
    assert result["metrics"]["sql"]["correct"] is False
    assert any(not check["passed"] for check in result["metrics"]["sql"]["checks"])


def test_wrong_time_range_is_rejected_by_sql_metric():
    case = deepcopy(_case("time_range_guard"))
    case["prediction"]["sql"] = case["prediction"]["sql"].replace("2024-01-01", "2023-01-01")
    result = evaluate_case(case)
    assert result["metrics"]["sql"]["correct"] is False


def test_missing_validation_issue_and_evidence_are_rejected():
    case = deepcopy(_case("empty_result"))
    case["prediction"]["validation"]["issues"] = []
    case["prediction"]["evidence"][0].pop("created_at")
    result = evaluate_case(case)
    assert result["metrics"]["validation"]["correct"] is False
    assert result["metrics"]["traceability"]["correct"] is False
