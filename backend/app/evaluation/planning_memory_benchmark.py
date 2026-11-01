"""Measure actual planner use of previously confirmed constraints, with/without memory."""
import argparse
import json
from pathlib import Path
from app.agent.analysis_plan import make_analysis_plan_graph
from app.core.factories import build_llm
from app.meta.store import get_llm_secret
from app.usage.callback import RequestUsage, with_request_usage

CASES = [
    ("tenant", "按上次确认的租户范围统计sales。", "已确认租户过滤tenant_code='TENANT-X731'，不得替换其他租户。", "TENANT-X731"),
    ("time", "按上次确认的时间范围统计sales。", "已确认本轮时间范围2026-02-07到2026-02-19。", "2026-02-19"),
    ("channel", "按之前确认的渠道过滤统计sales。", "已确认渠道筛选channel='RESELLER-Q59'。", "RESELLER-Q59"),
    ("correction", "按我现在修正的渠道DIRECT-Z82统计sales，旧渠道停用。", "之前默认渠道channel='RESELLER-Q59'，这条历史可能过期。", "DIRECT-Z82"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()
    cfg = get_llm_secret(3)
    llm = build_llm({**cfg, "temperature": 0})
    catalog = [{"name": "sales", "columns": ["sales", "tenant_code", "channel", "created_at"]}]
    report = {"model": cfg["model_name"], "reasoning_effort": cfg.get("reasoning_effort", "low"), "records": [], "limitations": ["synthetic constraint retention, not SQL execution accuracy", "without-memory ablation reproduces previous planner inputs; same planner code/model"]}
    for rep in range(args.repeat):
        for case_id, question, memory, target in CASES:
            for mode in ("without_memory", "with_memory"):
                usage = RequestUsage()
                plan = make_analysis_plan_graph(with_request_usage(llm, usage)).invoke({
                    "question": question, "catalog": catalog, "memory_context": memory if mode == "with_memory" else "",
                    "plan": None, "error": None})["plan"]
                payload = plan.model_dump(exclude_none=True)
                serialized = json.dumps(payload, ensure_ascii=False)
                # Check actual executable intent, excluding copies in question/goal.
                intent = json.dumps({k: payload.get(k) for k in ("filters", "metrics", "time_range")}, ensure_ascii=False)
                passed = target in intent and not plan.error
                if case_id == "correction":
                    passed = passed and "RESELLER-Q59" not in intent
                report["records"].append(dict(case_id=case_id, repeat=rep+1, mode=mode, passed=passed,
                    plan=payload, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens))
                out = Path(args.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(case_id, mode, rep+1, passed, flush=True)
    report["summary"] = {}
    for mode in ("without_memory", "with_memory"):
        rows = [r for r in report["records"] if r["mode"] == mode]
        report["summary"][mode] = {"runs": len(rows), "passed": sum(r["passed"] for r in rows),
            "success_rate": sum(r["passed"] for r in rows)/len(rows)}
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()
