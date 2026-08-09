"""运行离线分析评测：python -m app.evaluation"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

from app.evaluation.evaluator import evaluate_dataset, load_cases


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="运行 DataAnalysis Agent 离线评测集")
    parser.add_argument("--cases", help="评测集 JSON 路径，默认使用内置黄金样例")
    parser.add_argument("--output", help="可选：将完整 JSON 报告写入文件")
    parser.add_argument("--min-rate", type=float, default=1.0,
                        help="四项指标最低通过率，默认 1.0（CI 回归门禁）")
    args = parser.parse_args(argv)

    report = evaluate_dataset(load_cases(args.cases))
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "case_count": report["case_count"],
        "overall_rate": report["overall_rate"],
        "metrics": report["metrics"],
        "all_cases_passed": report["all_cases_passed"],
    }, ensure_ascii=False, indent=2))
    return 0 if all(item["rate"] >= args.min_rate for item in report["metrics"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
