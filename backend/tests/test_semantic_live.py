from app.evaluation.semantic_live import correct_execution, rows_equal
from app.evaluation.semantic_cases import load_frozen_cases


def test_numeric_oracle_is_order_independent_but_strict_on_cardinality():
    assert rows_equal([["b", .5], ["a", 90]], [["a", 90.0], ["b", .5]])
    assert not rows_equal([["a", 90], ["a", 90]], [["a", 90]])
    assert not rows_equal([["a", float("nan")]], [["a", 90]])
    assert rows_equal([[.5, "b"], [90, "a"]], [["a", 90.0], ["b", .5]])


def test_numerically_equal_wrong_source_is_not_a_correct_repair():
    case = next(c for c in load_frozen_cases("tune") if c.category == "wrong_source")
    # Wrong-source ratios can coincide; numeric coincidence must not pass.
    row = {"error": None, "submitted_sql": case.sql, "rows": list(case.expected_rows)}
    assert not correct_execution(case, [row])
