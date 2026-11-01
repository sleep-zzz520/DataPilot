"""Run the frozen SQL corpus through the original and contract-bound runtimes.

No model calls; this is a same-input semantic gate ablation. Every SQL candidate
is actually executed in DuckDB for the original path and compared to a separate
pandas oracle. The contract path executes only candidates its preflight permits.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

from app.evaluation.semantic_cases import (
    DEFAULT_MANIFEST, SUITE_DIR, SemanticCase, execute_case_sql, freeze_manifest,
    independent_expected, load_frozen_cases,
)


DEFAULT_BASELINE = SUITE_DIR / "baseline_sources/agent/analysis_plan.py"


def _load_baseline(path: Path):
    name = "_semantic_frozen_original_analysis_plan"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load baseline: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def rows_match_expected(case: SemanticCase, execution: dict) -> bool:
    if execution.get("error"):
        return False
    if list(execution["columns"]) != list(case.expected_columns):
        return False
    expected = independent_expected(case)
    actual = execution["rows"]
    if len(actual) != len(expected):
        return False
    # Outputs consist of dimension + numeric metric; compare order independently.
    try:
        expected, actual = sorted(expected), sorted(actual)
        return all(a[0] == e[0] and math.isclose(float(a[1]), float(e[1]), rel_tol=1e-9, abs_tol=1e-9)
                   for a, e in zip(actual, expected))
    except (TypeError, ValueError):
        return False


def _plan(module, case: SemanticCase):
    contract = case.contract_data
    return module.AnalysisPlan(goal=case.question, data_sources=[contract["source"]],
        metrics=[contract["name"]], dimensions=contract["group_by"], filters=contract["required_filters"],
        time_range=f"{case.start}/{case.end}", grain=contract["grain"], unit=contract["unit"],
        steps=[dict(id="query", kind="query", title="Query synthetic source"),
               dict(id="validate", kind="validate", title="Validate evidence", depends_on=["query"]),
               dict(id="summarize", kind="summarize", title="Summarize", depends_on=["validate"])])


def _tool_result(execution):
    if execution["error"]:
        return "SQL 执行错误：" + execution["error"]
    # This is the actual query payload, not predictions or the oracle's rows.
    payload = {key: execution[key] for key in ("columns", "rows", "executed_sql")}
    # Existing runtime protocol calls the file connector 'df'. Actual physical
    # table provenance is separate; every execution is genuinely DuckDB/pandas.
    payload.update(source="df", physical_sources=execution["sources"], execution_engine="duckdb")
    return "<!--TABLE:" + json.dumps(payload, ensure_ascii=False) + "-->"


def run_case(case: SemanticCase, baseline_module) -> dict:
    from app.agent import analysis_plan as current
    from app.agent.contracts import MetricContractRegistry, bind_metric_contracts
    execution = execute_case_sql(case)
    args = {"sql": case.sql}
    columns = next(item["columns"] for item in case.catalog if item["name"] == case.contract_data["source"])
    original = baseline_module.PlanRuntime(_plan(baseline_module, case), case.question, columns=columns)
    original.before_tool("query_file", args)
    original.after_tool("query_file", args, _tool_result(execution))
    original_validation = original.validate_evidence()
    before_action = "execute" if original_validation["status"] == "approved" else "repair"

    plan = _plan(current, case)
    registry = MetricContractRegistry([case.contract_data], [case.contract_data["id"]])
    binding = bind_metric_contracts(case.question, plan, case.catalog, registry,
                                    contract_ids=[case.contract_data["id"]])
    runtime = current.PlanRuntime(plan, case.question, columns=columns, binding=binding, catalog=case.catalog)
    decision = runtime.before_tool("query_file", args)
    after_execution = None
    if decision["allowed"]:
        # Execute a second time so the after path also has a real, auditable run.
        after_execution = execute_case_sql(case)
        runtime.after_tool("query_file", args, _tool_result(after_execution))
        after_validation = runtime.validate_evidence()
        after_action = "execute" if after_validation["status"] == "approved" else runtime.next_action()
    else:
        after_action = decision["action"]
        after_validation = {"status": "rejected", "issues": decision["issues"]}
    numeric_correct = rows_match_expected(case, execution)
    return dict(case_id=case.id, split=case.split, schema=case.schema, category=case.category,
        expected_action=case.expected_action, expected_rows=[list(row) for row in case.expected_rows],
        oracle_match=numeric_correct, candidate_execution=execution,
        before=dict(action=before_action, intercepted=before_action != "execute",
                    executed_sql=execution["executed_sql"], source=execution["source"],
                    validation=original_validation),
        after=dict(action=after_action, intercepted=after_action != "execute",
                   executed_sql=after_execution["executed_sql"] if after_execution else None,
                   source=after_execution["source"] if after_execution else None,
                   validation=after_validation, preflight=decision,
                   numeric_correct=rows_match_expected(case, after_execution) if after_execution else None))


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def summarize(records):
    result = {}
    for mode in ("before", "after"):
        # Semantic interception only counts invalid SQL that really executes.
        invalid = [r for r in records if r["expected_action"] == "repair" and not r["candidate_execution"]["error"]]
        valid = [r for r in records if r["expected_action"] == "execute"]
        clarify = [r for r in records if r["expected_action"] == "clarify"]
        result[mode] = dict(
            runs=len(records), semantic_invalid_count=len(invalid), valid_count=len(valid), clarify_count=len(clarify),
            intercepted_semantic_invalid=sum(r[mode]["intercepted"] for r in invalid),
            interception_rate=_ratio(sum(r[mode]["intercepted"] for r in invalid), len(invalid)),
            false_intercepts=sum(r[mode]["intercepted"] for r in valid),
            false_interception_rate=_ratio(sum(r[mode]["intercepted"] for r in valid), len(valid)),
            correct_clarifications=sum(r[mode]["action"] == "clarify" for r in clarify),
            clarification_accuracy=_ratio(sum(r[mode]["action"] == "clarify" for r in clarify), len(clarify)),
            clarification_precision=_ratio(sum(r[mode]["action"] == "clarify" for r in clarify),
                                           sum(r[mode]["action"] == "clarify" for r in records)),
            extra_clarifications=sum(r[mode]["action"] == "clarify" and r["expected_action"] != "clarify" for r in records),
            action_accuracy=_ratio(sum(r[mode]["action"] == r["expected_action"] for r in records), len(records)),
            accepted_wrong_results=sum(r[mode]["action"] == "execute" and not r["oracle_match"] for r in records),
        )
    return result


def run_replay(*, manifest_path=DEFAULT_MANIFEST, baseline_path=DEFAULT_BASELINE, output_path=None) -> dict:
    # Freeze and verify the entire corpus before looking at any validator outcome.
    manifest = freeze_manifest(Path(manifest_path))
    cases = load_frozen_cases(path=Path(manifest_path))
    baseline_path = Path(baseline_path)
    baseline = _load_baseline(baseline_path)
    agent_dir = Path(__file__).resolve().parents[1] / "agent"
    after_source_hashes = {name: hashlib.sha256((agent_dir / name).read_bytes()).hexdigest()
                           for name in ("analysis_plan.py", "contracts.py")}
    records = [run_case(case, baseline) for case in cases]
    report = dict(suite=manifest["suite"], experiment="same SQL + same synthetic data, original runtime vs AST-bound runtime",
        manifest_sha256=manifest["content_sha256"], baseline_path=str(baseline_path),
        baseline_sha256=hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        after_source_sha256=after_source_hashes,
        fixture_source_sha256=hashlib.sha256(Path(__file__).with_name("semantic_cases.py").read_bytes()).hexdigest(),
        oracle="independent pandas grouped arithmetic + human-frozen action labels",
        summary=summarize(records),
        splits={split: summarize([r for r in records if r["split"] == split]) for split in ("tune", "holdout")},
        records=records,
        limitations=["Synthetic semantics and finite supported SQL subset; not a production accuracy estimate.",
                    "No model calls or token/cost claims. Clarify cases are scored separately from executable semantic errors.",
                    "query_file adapter uses source=df as the runtime connector marker; execution records retain actual physical table sources and engine=duckdb.",
                    "Original runtime can reject after SQL execution; contract runtime can reject before execution."])
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--out", type=Path, default=SUITE_DIR / "semantic-replay.json")
    args = parser.parse_args()
    report = run_replay(manifest_path=args.manifest, baseline_path=args.baseline, output_path=args.out)
    print(json.dumps({"summary": report["summary"], "output": str(args.out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
