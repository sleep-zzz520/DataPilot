"""Reproducible live-model architecture benchmark on public synthetic data.

Run: python -m app.evaluation.architecture_benchmark --repeat 2 --out data/eval/memory-agent-20261031.json
No production uploads, history, business database, or LLM judge is used.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from langchain_core.messages import HumanMessage, SystemMessage
from app.agent.analysis_plan import AnalysisPlan, PlanRuntime, plan_context
from app.agent.multi_agent import make_agent
from app.agent.prompts import CONVERSATION_HINT
from app.agent.trace import TraceCollector
from app.core.factories import build_llm
from app.meta.store import get_llm_secret
from app.tools.agent_tools import make_tools
from app.usage.callback import RequestUsage, with_request_usage


def dataset():
    return pd.DataFrame([
        {"month": month, "region": region, "sales": 100 * m + 25 * r,
         "orders": 10 * m + r, "refund": 5 * m + r}
        for m, month in enumerate(("2026-01", "2026-02", "2026-03"), 1)
        for r, region in enumerate(("华东", "华南", "华北"), 1)
    ])


def cases(frame):
    # Answers are derived independently with pandas, never supplied to the model.
    return [
        {"id": "sum", "question": "sales总和是多少？只给这个数值。", "values": [int(frame.sales.sum())]},
        {"id": "orders", "question": "orders总和是多少？只给这个数值。", "values": [int(frame.orders.sum())]},
        {"id": "east", "question": "华东区域sales总和是多少？只给这个数值。", "values": [int(frame[frame.region == "华东"].sales.sum())]},
        {"id": "march", "question": "2026-03的sales总和是多少？只给这个数值。", "values": [int(frame[frame.month == "2026-03"].sales.sum())]},
        {"id": "refund", "question": "refund总和是多少？只给这个数值。", "values": [int(frame.refund.sum())]},
        {"id": "count", "question": "文件共有多少条记录？只给记录数。", "values": [len(frame)]},
        {"id": "compare", "question": "分别计算华东和华南的sales总和，并计算两者差额。", "values": [675, 750, 75], "complex": True},
        {"id": "period", "question": "分别计算2026-01和2026-03的sales总和，再计算增长金额。", "values": [450, 1050, 600], "complex": True},
        {"id": "gender", "question": "按客户性别统计sales总和。", "decline": True},
        {"id": "profit", "question": "净利润总额是多少？", "decline": True},
        {"id": "absent", "question": "西南区域的sales是多少？", "decline": True},
        {"id": "year", "question": "2025年的sales同比增长率是多少？", "decline": True},
    ]


def score(case, reply, error):
    if error or not reply.strip():
        return False
    refusal = any(s in reply for s in ("无法", "不足", "缺少", "不包含", "没有", "不存在", "未通过", "未能", "不支持"))
    # Fail closed: refusal plus invented numbers isn't a successful supported answer.
    if case.get("decline"):
        # Dates and column names in a refusal are allowed; numeric business claims aren't.
        labels = {"gender": r"男(?:性|生)?|女(?:性|生)?", "profit": "净利润",
                  "absent": "西南(?:区域)?", "year": "同比增长率"}.get(case.get("id"), "总额|总和|利润|销售额|sales|增长率")
        claims = re.findall(r"(?:" + labels + r")\s*(?:销售额|sales)?\s*(?:为|是|[:：=])\s*[-+]?\d", reply, re.I)
        return refusal and not claims
    if any(s in reply for s in ("无法给出", "无法计算", "未通过", "暂不生成", "未能完成", "缺少所需")):
        return False
    numbers = [float(s.replace(",", "")) for s in re.findall(r"(?<![A-Za-z0-9_])[-+]?\d[\d,]*(?:\.\d+)?", reply)]
    return all(any(abs(float(expected) - n) < 0.011 for n in numbers) for expected in case["values"])


def make_plan(case):
    return AnalysisPlan(goal=case["question"], data_sources=["sales.csv"], metrics=["sales"], steps=[
        {"id": "q", "title": "查询真实文件", "kind": "query"},
        *([{"id": "q2", "title": "第二项独立查询", "kind": "query", "depends_on": ["q"]}] if case.get("complex") else []),
        {"id": "v", "title": "校验结果", "kind": "validate", "depends_on": ["q"]},
        {"id": "s", "title": "给出结论", "kind": "summarize", "depends_on": ["v"]},
    ])


def load_baseline(root):
    import app.agent.multi_agent as current
    def load(name):
        spec = importlib.util.spec_from_file_location("app.agent._baseline_" + name, Path(root) / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    graph = load("graph")
    module = load("multi_agent")
    module.make_graph = graph.make_graph
    return module.make_agent


def run_one(mode, case, llm, tools, frame, baseline):
    usage = RequestUsage()
    model = with_request_usage(llm, usage)
    plan = make_plan(case)
    runtime = PlanRuntime(plan, case["question"], columns=list(frame.columns))
    trace = TraceCollector()
    kwargs = dict(trace=trace, plan_runtime=runtime)
    if mode == "baseline_multi":
        graph, prompt = baseline(model, tools, **kwargs)
    else:
        graph, prompt = make_agent(model, tools, mode=mode, question=case["question"], **kwargs)
    context = ("文件sales.csv，真实列：" + str(list(frame.columns)) +
               "。先调用query_file读取真实数据。禁止根据列名推断缺失数据。")
    messages = [SystemMessage(content=prompt + "\n" + CONVERSATION_HINT + "\n" + plan_context(plan)),
                HumanMessage(content=context + "\n" + case["question"])]
    start = time.monotonic()
    error = None
    reply = ""
    try:
        result = graph.invoke({"messages": messages}, config={"recursion_limit": 48})
        reply = str(result["messages"][-1].content)
    except Exception as exc:
        # Never serialize provider error bodies (they may contain request credentials).
        error = type(exc).__name__
    elapsed = time.monotonic() - start
    return dict(case_id=case["id"], mode=mode, question=case["question"], reply=reply,
                passed=score(case, reply, error), error=error, latency_s=round(elapsed, 3),
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                llm_calls=usage.calls, validation_status=runtime.validation_status,
                trace=trace.snapshot(), evidence_count=len(plan.evidence))


def summarize(records):
    result = {}
    for mode in sorted({r["mode"] for r in records}):
        rows = [r for r in records if r["mode"] == mode]
        result[mode] = {"runs": len(rows), "passed": sum(r["passed"] for r in rows),
                        "success_rate": round(sum(r["passed"] for r in rows) / len(rows), 4),
                        "errors": sum(bool(r["error"]) for r in rows)}
        for key in ("latency_s", "input_tokens", "output_tokens", "llm_calls"):
            result[mode]["mean_" + key] = round(sum(r[key] for r in rows) / len(rows), 3)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--config-id", type=int, default=3)
    parser.add_argument("--modes", default="baseline_multi,single,multi,auto")
    parser.add_argument("--cases", default="")
    parser.add_argument("--baseline-root", default=str(Path(__file__).resolve().parents[2] / "data/eval/20261031/baseline_sources/agent"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.repeat < 1 or not 1 <= args.workers <= 2:
        parser.error("repeat must be positive")
    cfg = get_llm_secret(args.config_id)
    llm = build_llm({**cfg, "temperature": 0})
    frame = dataset()
    selected = [c for c in cases(frame) if not args.cases or c["id"] in args.cases.split(",")]
    modes = args.modes.split(",")
    if not selected or any(m not in {"baseline_multi", "single", "multi", "auto"} for m in modes):
        parser.error("invalid cases or modes")
    tools = [t for t in make_tools(None, files={"sales.csv": frame}) if t.name in {"query_file", "list_files", "file_stats"}]
    baseline = load_baseline(args.baseline_root) if "baseline_multi" in modes else None
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    report = dict(model=cfg.get("model_name"), reasoning_effort=cfg.get("reasoning_effort", "low"), temperature=0, seed=31,
                  data_sha256=hashlib.sha256(frame.to_csv(index=False).encode()).hexdigest(),
                  dataset=frame.to_dict("records"), cases=selected, repeat=args.repeat,
                  limitations=["synthetic file workload; no MySQL or UI acceptance", "lexical refusal and numeric oracle; not semantic proof", "shared fixed plan isolates execution; planner not part of this experiment"],
                  records=[])
    app_root = Path(__file__).resolve().parents[1]
    report["source_sha256"] = {str(p.relative_to(app_root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [app_root / "agent/graph.py", app_root / "agent/multi_agent.py",
                  app_root / "agent/analysis_plan.py", app_root / "core/factories.py",
                  app_root / "memory/context.py"]}
    report["max_output_tokens"] = cfg.get("max_tokens")
    report["expected_runs"] = len(selected) * len(modes) * args.repeat
    schedule = [(case, mode, rep) for case in selected for mode in modes for rep in range(args.repeat)]
    random.Random(31).shuffle(schedule)
    if args.resume and out.exists():
        prior = json.loads(out.read_text())
        if prior["model"] != report["model"] or prior["data_sha256"] != report["data_sha256"]:
            parser.error("resume model/data mismatch")
        report["records"] = prior["records"]
    report["workers"] = args.workers
    report["scorer_version"] = 2
    by_id = {case["id"]: case for case in selected}
    for row in report["records"]:
        row.setdefault("initial_passed", row["passed"])
        row["passed"] = score(by_id[row["case_id"]], row["reply"], row["error"])
    completed = {(r["case_id"], r["mode"], r["repeat"]) for r in report["records"]}
    schedule = [(c, m, rep) for c, m, rep in schedule if (c["id"], m, rep + 1) not in completed]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(run_one, mode, case, llm, tools, frame, baseline): (case, mode, rep)
                   for case, mode, rep in schedule}
        for future in as_completed(pending):
            case, mode, rep = pending[future]
            row = future.result()
            row["repeat"] = rep + 1
            report["records"].append(row)
            report["summary"] = summarize(report["records"])
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(case["id"], mode, rep + 1, row["passed"], row["latency_s"], row["input_tokens"], flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
