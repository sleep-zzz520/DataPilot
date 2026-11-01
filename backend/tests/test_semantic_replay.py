"""Actual SQL execution and independent business arithmetic guard the benchmark."""
import json

import pytest

from app.evaluation.semantic_cases import (
    build_cases, execute_case_sql, freeze_manifest, independent_expected,
    live_cases, load_frozen_cases,
)
from app.evaluation.semantic_replay import DEFAULT_BASELINE, rows_match_expected, run_replay


def test_fixture_splits_cover_different_metrics_and_real_schemas():
    cases = build_cases()
    assert len({c.id for c in cases}) == len(cases) == 32
    for split in ("tune", "holdout"):
        selected = [c for c in cases if c.split == split]
        assert len(selected) >= 12
        assert {c.schema for c in selected} == {"commerce", "operations"}
        assert {c.expected_action for c in selected} == {"execute", "repair", "clarify"}
    assert {c.oracle_kind for c in cases if c.split == "tune"}.isdisjoint(
        {c.oracle_kind for c in cases if c.split == "holdout"})
    assert {c.category for c in cases} >= {"or_weakened_time", "unit_multiplier", "join_fanout",
                                         "valid_alias_reordered", "valid_safe_join", "missing_required_column"}


def test_manifest_is_content_hashed_and_cannot_be_rewritten(tmp_path):
    path = tmp_path / "manifest.json"
    first = freeze_manifest(path)
    assert first == freeze_manifest(path)
    assert len(first["content_sha256"]) == 64
    assert len(first["case_hashes"]) == 32
    original = path.read_bytes()
    raw = json.loads(original)
    next(c for c in raw["cases"] if c["split"] == "holdout" and c["expected_action"] == "repair")["expected_action"] = "execute"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="Frozen case manifest changed"):
        load_frozen_cases(path=path)


@pytest.mark.parametrize("case", [c for c in build_cases() if c.expected_action == "execute"], ids=lambda c: c.id)
def test_valid_sql_really_executes_and_matches_independent_pandas(case):
    execution = execute_case_sql(case)
    assert execution["error"] is None
    assert execution["rows"] != []
    assert rows_match_expected(case, execution)
    assert independent_expected(case) == [list(row) for row in case.expected_rows]
    assert execution["executed_sql"] == case.sql
    assert execution["source"] == case.contract_data["source"]
    assert execution["execution_engine"] == "duckdb"


@pytest.mark.parametrize("category", ["missing_time", "wrong_time", "or_weakened_time", "wrong_expression",
                                      "unit_multiplier", "wrong_grain", "wrong_denominator", "join_fanout"])
def test_wrong_queries_run_but_produce_business_differences(category):
    cases = [c for c in build_cases() if c.category == category]
    assert cases
    for case in cases:
        execution = execute_case_sql(case)
        assert execution["error"] is None, execution["error"]
        assert not rows_match_expected(case, execution)


def test_missing_fields_fail_execution_and_have_frozen_clarify_labels():
    cases = [c for c in build_cases() if c.missing_column]
    assert len(cases) == 4
    for case in cases:
        assert case.expected_action == "clarify"
        assert execute_case_sql(case)["error"]


def test_live_subset_is_stratified_and_excludes_gold_context(tmp_path):
    cases = live_cases(path=tmp_path / "manifest.json")
    assert len(cases) == 16
    assert {c.split for c in cases} == {"tune", "holdout"}
    assert {c.schema for c in cases} == {"commerce", "operations"}
    assert {c.category for c in cases} >= {"unit_multiplier", "join_fanout", "or_weakened_time", "missing_required_column"}
    for case in cases:
        context = case.model_context()
        assert "sql" not in context and "expected_action" not in context and "expected_rows" not in context


def test_executor_restricts_generated_candidates_to_local_read_only_select():
    case = build_cases()[0]
    for sql in ("SELECT 1; DROP TABLE orders", "SELECT * FROM read_csv('/etc/passwd')",
                "ATTACH '/tmp/unknown' AS other", "SELECT * FROM information_schema.tables"):
        assert execute_case_sql(case, sql)["error"]


@pytest.mark.skipif(not DEFAULT_BASELINE.exists(), reason="Frozen original runtime snapshot is local evaluation material")
def test_same_input_replay_uses_actual_baseline_and_runtime(tmp_path):
    report = run_replay(manifest_path=tmp_path / "manifest.json", output_path=tmp_path / "report.json")
    assert len(report["records"]) == 32
    assert report["baseline_sha256"]
    for split in ("tune", "holdout"):
        before = report["splits"][split]["before"]
        after = report["splits"][split]["after"]
        assert after["intercepted_semantic_invalid"] > before["intercepted_semantic_invalid"]
        assert after["false_intercepts"] == 0
        assert after["correct_clarifications"] == after["clarify_count"]
    for record in report["records"]:
        if record["expected_action"] == "execute":
            assert record["after"]["executed_sql"] == record["candidate_execution"]["executed_sql"]
            assert record["after"]["numeric_correct"]
        elif record["after"]["intercepted"]:
            assert record["after"]["executed_sql"] is None
