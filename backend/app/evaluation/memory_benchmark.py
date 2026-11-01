"""Live repeated-compaction retention evaluation; uses disposable SQLite only."""
from __future__ import annotations
import argparse
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from app import persistence
from app.memory import agent_memory
from app.core.factories import build_llm
from app.meta.store import get_llm_secret
from app.usage.callback import RequestUsage, with_request_usage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--config-id", type=int, default=3)
    parser.add_argument("--baseline-root", default=str(Path(__file__).resolve().parents[2] / "data/eval/20261031/baseline_sources/memory"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = get_llm_secret(args.config_id)
    model = build_llm({**cfg, "temperature": 0})
    spec = importlib.util.spec_from_file_location("app.memory._baseline_memory", Path(args.baseline_root) / "agent_memory.py")
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {"model": cfg.get("model_name"), "reasoning_effort": cfg.get("reasoning_effort", "low"), "repeat": args.repeat, "records": [],
              "limitations": ["synthetic 15-turn conversation; 3 compactions; not real user traffic", "retention measured by exact unique facts; not semantic or causal proof", "maintenance tokens are included"]}
    for repeat in range(args.repeat):
        for name, module in (("baseline", baseline), ("optimized", agent_memory)):
            with tempfile.TemporaryDirectory(prefix="agent-memory-eval-") as directory:
                persistence._DB_DIR = Path(directory)
                persistence._DB_PATH = Path(directory) / "history.db"
                session = "retention"
                history = []
                usage = RequestUsage()
                llm = with_request_usage(model, usage)
                snapshots = []
                start = time.monotonic()
                for turn in range(15):
                    text = ("本次分析约定：业务代号ORCHID-731，币种CNY，排除测试订单TEST-ONLY；待办是核对退款。"
                            if turn == 0 else f"检查第{turn}批数据的缺失率与重复行，记录本轮检查完成，不修改前面确认的口径和待办。")
                    pair = [HumanMessage(content=text, id=f"u-{turn}"),
                            AIMessage(content=f"第{turn}批检查完成，继续下一批。", id=f"a-{turn}")]
                    history.extend(pair)
                    persistence.persist_messages(session, pair)
                    if (turn + 1) % 5 == 0:
                        summary = module.generate_summary(llm, session, history) or ""
                        snapshots.append(summary)
                facts = ["ORCHID-731", "CNY", "TEST-ONLY", "退款"]
                final = snapshots[-1]
                report["records"].append({"mode": name, "repeat": repeat + 1,
                    "snapshots": snapshots, "retained": {fact: fact in final for fact in facts},
                    "retention_rate": sum(fact in final for fact in facts) / len(facts),
                    "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                    "calls": usage.calls, "seconds": round(time.monotonic() - start, 3)})
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(name, repeat + 1, report["records"][-1]["retention_rate"], usage.input_tokens, flush=True)
    report["summary"] = {}
    for name in ("baseline", "optimized"):
        rows = [r for r in report["records"] if r["mode"] == name]
        report["summary"][name] = {"runs": len(rows), **{
            "mean_" + k: round(sum(r[k] for r in rows) / len(rows), 4)
            for k in ("retention_rate", "input_tokens", "output_tokens", "calls", "seconds")}}
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
