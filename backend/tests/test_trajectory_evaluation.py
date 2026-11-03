from copy import deepcopy

import pytest

from app.evaluation.quality_scoring import score_trial
from app.evaluation.task_suite import load_suite, validate_task
from app.evaluation.trajectory import trajectory_checks


def query(file, sql="SELECT x FROM df", status="ok"):
    call = {"tool": "query_file", "arguments": {"file": file, "sql": sql}, "status": status}
    if status == "ok":
        call["table"] = {"columns": ["x"], "rows": [[1]]}
    return call


def test_distinguishes_actual_source_read_from_attempt_and_preserves_equivalent_order():
    spec = {"required_file_reads": ["a.csv", "b.csv"], "max_file_queries": 3}
    assert all(passed for _, passed, known in trajectory_checks(spec, [query("b.csv"), query("a.csv")]))
    checks = trajectory_checks(spec, [query("a.csv"), query("b.csv", status="error")])
    assert checks == [("trajectory:required_file_reads", False, True), ("trajectory:max_file_queries", True, True)]


def test_repeat_of_failed_sql_is_detected_even_if_format_changes():
    spec = {"no_repeat_failed_sql": True}
    failed = query("a.csv", "select x from df;", "error")
    unchanged = query("a.csv", "SELECT   x FROM df")
    changed = query("a.csv", "SELECT y FROM df")
    assert trajectory_checks(spec, [failed, unchanged]) == [("trajectory:no_repeat_failed_sql", False, True)]
    assert trajectory_checks(spec, [failed, changed]) == [("trajectory:no_repeat_failed_sql", True, True)]


def test_injected_timeout_is_not_inferred_from_reply_or_partial_trace():
    spec = {"require_query_timeout": True, "max_file_queries": 3}
    assert trajectory_checks(spec, None)[0] == ("trajectory:require_query_timeout", False, False)
    assert trajectory_checks(spec, [query("a.csv", status="running")])[0][2] is False
    assert trajectory_checks(spec, [query("a.csv", status="error")])[0] == ("trajectory:require_query_timeout", False, True)
    assert trajectory_checks(spec, [query("a.csv", status="timeout")])[0] == ("trajectory:require_query_timeout", True, True)


def test_missing_trajectory_cannot_pass_and_schema_rejects_invalid_source():
    task = next(t for t in load_suite()["tasks"] if t["id"] == "reg_join")
    row = score_trial(task, {})
    check = next(c for c in row["checks"] if c["name"] == "trajectory:required_file_reads")
    assert check["status"] == "unknown" and not row["passed"]
    bad = deepcopy(task)
    bad["expected"]["trajectory"]["required_file_reads"] = ["not_uploaded.csv"]
    with pytest.raises(ValueError, match="fixture"):
        validate_task(bad)


def test_timeout_checkpoint_proves_only_observed_facts():
    spec = {"required_file_reads": ["a.csv", "b.csv"], "max_file_queries": 3, "no_repeat_failed_sql": True}
    read = query("a.csv")
    read.pop("table")
    read["table_observed"] = True
    pending = query("b.csv", status="running")
    checks = trajectory_checks(spec, [read, pending], complete=False)
    assert not any(known for _, _, known in checks)
    checks = trajectory_checks(spec, [read, query("b.csv"), pending], complete=False)
    assert checks[0] == ("trajectory:required_file_reads", True, True)
    assert not checks[1][2]


@pytest.mark.parametrize("sql", ["SELECT 1", "SELECT * FROM (WITH df AS (SELECT 1) SELECT * FROM df)"])
def test_constant_or_shadowed_dataframe_is_not_a_source_read(sql):
    spec = {"required_file_reads": ["a.csv"]}
    assert trajectory_checks(spec, [query("a.csv", sql)]) == [("trajectory:required_file_reads", False, True)]
