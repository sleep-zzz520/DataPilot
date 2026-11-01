"""Live-model controlled fault recovery, using frozen cases and pandas answers.

The same first SQL proposal is injected in every arm; it is NOT a model call.
Subsequent decisions and repairs use the configured real GLM model. This measures
conditional error detection/recovery, not spontaneous natural-language accuracy.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import math
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage
from langchain_core.tools import tool
from app.agent.analysis_plan import AnalysisPlan, PlanRuntime, plan_context
from app.agent.contracts import ContractBinding
from app.agent.graph import make_graph
from app.agent.prompts import SINGLE_AGENT_PROMPT
from app.agent.trace import TraceCollector
from app.core.factories import build_llm
from app.meta.store import get_llm_secret
from app.usage.callback import RequestUsage, with_request_usage
from app.evaluation.semantic_cases import live_cases, execute_case_sql, freeze_manifest, SUITE_DIR


class InjectedFirstProposal:
    def __init__(self, model, statement):
        self.model, self.statement, self.used = model, statement, False
        self.max_tokens = model.max_tokens

    def bind_tools(self, tools):
        parent, real = self, self.model.bind_tools(tools)
        class Bound:
            def stream(self, messages, config):
                if not parent.used:
                    parent.used = True
                    yield AIMessageChunk(content="", tool_calls=[{"name": "query_mysql",
                        "args": {"sql": parent.statement}, "id": "controlled-initial-proposal"}])
                else:
                    yield from real.stream(messages, config)
        return Bound()


def original_runtime():
    path = SUITE_DIR / "baseline_sources/agent/analysis_plan.py"
    spec = importlib.util.spec_from_file_location("app.agent._semantic_baseline", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rows_equal(actual, expected):
    # Frozen corpus: one textual dimension and one numeric metric. SQL may
    # legally reorder those projections or rename their aliases.
    def normalize(row):
        if (len(row) == 2 and isinstance(row[0], (int, float))
                and isinstance(row[1], str)):
            return [row[1], row[0]]
        return row
    actual = [normalize(row) for row in actual]
    expected = [normalize(row) for row in expected]
    def key(row):
        return str(row[0]) if row else ""
    actual, expected = sorted(actual, key=key), sorted(expected, key=key)
    if len(actual) != len(expected):
        return False
    for left, right in zip(actual, expected):
        if len(left) != len(right):
            return False
        for a, b in zip(left, right):
            if isinstance(b, (int, float)):
                if not isinstance(a, (int, float)) or not math.isfinite(a) or not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6):
                    return False
            elif str(a) != str(b):
                return False
    return True


def correct_execution(case, executions):
    """Numeric oracle plus independently checked source identity and actual repair."""
    import sqlglot
    from sqlglot import exp
    successes = [row for row in executions if not row.get("error")]
    if not successes:
        return False
    final = successes[-1]
    sql = final["submitted_sql"]
    try:
        tables = {table.name.lower() for table in sqlglot.parse_one(sql, read="mysql").find_all(exp.Table)}
    except Exception:
        return False
    source = case.contract.source.split(".")[-1].lower()
    if source not in tables:
        return False
    if case.expected_action == "repair" and sql.strip().rstrip(";") == case.sql.strip().rstrip(";"):
        return False
    return rows_equal(final["rows"], list(case.expected_rows))


def run_one(case, mode, model, baseline):
    usage = RequestUsage()
    wrapped = InjectedFirstProposal(with_request_usage(model, usage), case.sql)
    executed = []

    @tool
    def query_mysql(sql: str) -> str:
        """Run one read-only SELECT on the available synthetic tables; return actual rows."""
        # mysql quote syntax is adapted for the local DuckDB execution adapter.
        import sqlglot
        try:
            executable = sqlglot.transpile(sql, read="mysql", write="duckdb")[0]
            outcome = execute_case_sql(case, executable)
        except Exception as exc:
            outcome = {"rows": [], "columns": [], "error": type(exc).__name__}
        executed.append({"submitted_sql": sql, **outcome})
        if outcome.get("error"):
            return "SQL 执行错误：" + str(outcome["error"])[:300]
        payload = {**outcome, "executed_sql": sql, "source": "mysql",
                   "row_count_total": len(outcome["rows"]), "truncated": False}
        return "真实查询结果：" + json.dumps(payload, ensure_ascii=False) + "\n<!--TABLE:" + json.dumps(payload, ensure_ascii=False) + "-->"

    data = dict(goal=case.question, metrics=[case.contract.id],
                filters=list(case.contract.required_filters), dimensions=list(case.contract.group_by),
                steps=[{"id": "query", "title": "按指标契约查询", "kind": "query"},
                       {"id": "verify", "title": "验证结果", "kind": "validate", "depends_on": ["query"]},
                       {"id": "answer", "title": "给出证据结论", "kind": "summarize", "depends_on": ["verify"]}])
    if mode == "contract":
        plan = AnalysisPlan(**data)
        from app.agent.contracts import MetricContractRegistry, bind_metric_contracts
        registry = MetricContractRegistry([case.contract], [case.contract.id])
        binding = bind_metric_contracts(case.question, plan, case.catalog, registry, [case.contract.id])
        runtime = PlanRuntime(plan, case.question, binding=binding, catalog=case.catalog)
    else:
        plan = baseline.AnalysisPlan(**data)
        runtime = baseline.PlanRuntime(plan, case.question)
    trace = TraceCollector()
    graph = make_graph(wrapped, [query_mysql], plan_runtime=runtime, trace=trace,
                       enforce_validation=True, max_sql_attempts=1 if mode == "direct" else 3,
                       max_model_calls=8)
    # All arms receive identical trusted definitions; baseline is allowed to self-correct.
    prompt = SINGLE_AGENT_PROMPT + "\n服务端指标契约：" + json.dumps(case.contract_data, ensure_ascii=False)
    prompt += "\n只使用query_mysql，最终给出各维度的真实数值；缺少必要字段请明确说明，不得替换公式。"
    messages = [SystemMessage(content=prompt), HumanMessage(content=json.dumps(case.model_context(), ensure_ascii=False))]
    started = time.monotonic()
    error, reply = None, ""
    try:
        result = graph.invoke({"messages": messages})
        reply = str(result["messages"][-1].content)
    except Exception as exc:
        error = type(exc).__name__
    successes = [row for row in executed if not row.get("error")]
    correct_result = correct_execution(case, executed)
    if case.expected_action == "clarify":
        refusal = any(word in reply for word in ("缺少", "无法", "不能", "确认", "口径", "补充"))
        passed = not error and refusal and not successes and runtime.validation_status != "approved"
    else:
        passed = not error and correct_result and runtime.validation_status == "approved"
    return {"case_id": case.id, "split": case.split, "schema": case.schema,
            "category": case.category, "expected_action": case.expected_action, "mode": mode,
            "passed": passed, "result_correct": correct_result, "error": error, "reply": reply,
            "latency_s": round(time.monotonic()-started, 3),
            "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "completed_provider_calls": usage.calls, "initial_proposal_injected": wrapped.used,
            "executed": executed, "validation_status": runtime.validation_status,
            "plan": plan.model_dump(exclude_none=True), "trace": trace.snapshot()}


def summarize(records):
    result = {}
    for split in ("tune", "holdout"):
        result[split] = {}
        for mode in ("direct", "retry", "contract"):
            rows = [r for r in records if r["split"] == split and r["mode"] == mode]
            if not rows:
                continue
            result[split][mode] = {"runs": len(rows), "passed": sum(r["passed"] for r in rows),
                "success_rate": sum(r["passed"] for r in rows)/len(rows),
                "errors": sum(bool(r["error"]) for r in rows),
                **{"mean_"+k: round(sum(r[k] for r in rows)/len(rows), 3)
                   for k in ("latency_s", "input_tokens", "output_tokens", "completed_provider_calls")}}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["tune", "holdout", "all"], default="tune")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--cases", default="")
    parser.add_argument("--modes", default="direct,retry,contract")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    manifest = freeze_manifest()
    selected = [c for c in live_cases(args.limit) if args.split == "all" or c.split == args.split]
    if args.cases:
        selected = [c for c in selected if c.id in args.cases.split(",")]
    if not selected or args.repeat < 1:
        parser.error("No cases or invalid repetitions")
    config = get_llm_secret(3)
    if str(config["model_name"]).lower() != "glm-5.3-flash":
        parser.error("Expected glm-5.3-Flash")
    model = build_llm({**config, "temperature": 0, "reasoning_effort": "low"})
    baseline = original_runtime()
    modes = args.modes.split(",")
    if any(m not in {"direct", "retry", "contract"} for m in modes):
        parser.error("Unknown mode")
    schedule = [(case, mode, rep) for case in selected for mode in modes for rep in range(args.repeat)]
    random.Random(20261031).shuffle(schedule)
    app = Path(__file__).resolve().parents[1]
    source_hashes = {str(p.relative_to(app)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in [app/"agent/contracts.py", app/"agent/analysis_plan.py", app/"agent/graph.py"]}
    report = {"model": config["model_name"], "temperature": 0, "reasoning_effort": "low",
        "scorer_version": 3,
        "case_manifest_sha256": manifest["content_sha256"], "source_sha256": source_hashes,
        "expected_runs": len(schedule), "workers": 2, "records": [],
        "limitations": ["Controlled first-SQL fault injection; subsequent repairs use real model",
                        "MySQL SQL adapted to DuckDB; not live MySQL validation",
                        "Pass requires executed result agrees with independent pandas oracle and gate approval; not proof of all prose claims"]}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(run_one, case, mode, model, baseline): (case, mode, rep) for case,mode,rep in schedule}
        for future in as_completed(futures):
            case, mode, rep = futures[future]
            row = future.result()
            row["repeat"] = rep+1
            report["records"].append(row)
            report["summary"] = summarize(report["records"])
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(case.id, mode, rep+1, row["passed"], row["error"], row["latency_s"], flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
