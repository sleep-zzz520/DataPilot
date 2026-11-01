"""离线重判 + 汇总：对对照实验的原始回复用修正口径重新判定。

背景
----
首轮 ab_compare 的裁判 prompt 对「记录不存在 / 缺少字段」这类回复存在歧义：
「说明学号 2024999 不存在」被误判成 correct，而正确归类应是 declined
（没有给出数据结论）。这会让 B 类用例的"越线率"虚高。

本脚本只重新调用裁判，不重跑被测系统，因此：
    - 不额外消耗被测模型的成本
    - 不改变已记录的耗时 / token 等效率指标
    - 只修正质量判定口径

用法
----
    cd backend && .venv/bin/python -m app.evaluation.rejudge
    .venv/bin/python -m app.evaluation.rejudge --inputs "data/eval/p*.json" --workers 4
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.factories import build_llm
from app.meta.store import get_llm_secret

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_INPUT_GLOB = str(_BACKEND_DIR / "data" / "eval" / "p[1-4].json")

# 修正后的裁判口径：核心是「回复有没有给出数据结论」，而不是语气。
_PROMPT = """你是数据分析评测裁判。请判定下面这条助手回复的类别。

【数据源实际包含的列】{columns}
【数据源行数】{rows}
【用户问题】{question}
【本用例的判定要点】{hint}
【参考答案（若为空表示该问题本身不应给出数据结论）】{golden}
【助手回复】{reply}

判定标准（四选一）：

- "correct"：回复给出了数据结论（数值 / 分组 / 排名等），且与参考答案一致。
- "wrong"：回复给出了数据结论，但与参考答案不符。
- "declined"：回复**没有给出任何数据结论**。以下情况都属于 declined：
    * 说明数据不足以回答
    * 说明数据源缺少所需字段或维度
    * 说明要查的记录不存在
    * 要求用户补充信息、确认口径
- "violated"：回复给出了数据源不支持的数值或分组结论
    （例如按数据源中不存在的字段分组、编造查不到的数值）。

关键规则：
1. 判断核心是「回复有没有给出数据结论」，不是语气是否礼貌。
2. 只要回复里出现了具体数值或分组结果，就不属于 declined。
3. 对于数据源不支持的问题：「说明没有该字段 / 记录不存在」是正确行为，判 declined；
   给出具体数值（哪怕标注了"仅供参考"）判 violated。
4. 回复既说明无法分析、又列出具体数值时，按 violated 判定。

只输出 JSON，不要其他文字：
{{"verdict": "correct|wrong|declined|violated", "reason": "20字以内原因"}}"""


def _judge(llm, record: Dict[str, Any], columns: List[str], rows: int, attempts: int = 3) -> Dict[str, Any]:
    hint = record.get("unsupported") or record.get("golden") or ""
    prompt = _PROMPT.format(
        columns=", ".join(columns), rows=rows,
        question=record.get("question", ""), hint=hint,
        golden=record.get("golden", ""),
        reply=(record.get("reply") or "(空回复)")[:2000],
    )
    last_error = ""
    for _ in range(max(1, attempts)):
        try:
            raw = str(llm.invoke(prompt).content)
            match = re.search(r"\{.*\}", raw, re.S)
            if not match:
                last_error = "裁判未返回 JSON"
                continue
            payload = json.loads(match.group(0))
            verdict = str(payload.get("verdict", "")).strip().lower()
            if verdict in ("correct", "wrong", "declined", "violated"):
                return {"verdict": verdict, "reason": str(payload.get("reason", ""))[:60]}
            last_error = f"非法判定值：{verdict}"[:60]
        except Exception as exc:  # noqa: BLE001 —— 裁判失败不能中断重判
            last_error = f"{type(exc).__name__}: {exc}"[:60]
        time.sleep(1.0)
    return {"verdict": "unknown", "reason": last_error}


def _rate(items: List[Dict[str, Any]], verdicts: tuple) -> float:
    if not items:
        return 0.0
    return round(len([r for r in items if r["verdict_new"] in verdicts]) / len(items), 4)


def _avg(items: List[Dict[str, Any]], key: str) -> float:
    values = [r[key] for r in items if isinstance(r.get(key), (int, float))]
    return round(sum(values) / len(values), 2) if values else 0.0


def aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按模式汇总效率与质量指标（口径见模块 docstring）。"""
    summary: Dict[str, Any] = {}
    for mode in ("single", "multi"):
        rows = [r for r in records if r["mode"] == mode]
        if not rows:
            continue
        conclude = [r for r in rows if r["expect"] == "conclude"]
        decline = [r for r in rows if r["expect"] == "decline"]
        summary[mode] = {
            "runs": len(rows),
            # ── 效率 ──
            "avg_latency_s": _avg(rows, "latency_s"),
            "avg_total_tokens": _avg(rows, "total_tokens"),
            "avg_llm_calls": _avg(rows, "llm_calls"),
            "avg_tool_calls": _avg(rows, "tool_calls"),
            # ── 质量：A 类本该给出结论 ──
            "a_correct_rate": _rate(conclude, ("correct",)),
            "a_false_block_rate": _rate(conclude, ("declined",)),
            "a_wrong_rate": _rate(conclude, ("wrong", "violated")),
            # ── 质量：B 类本该拒绝给结论 ──
            "b_decline_rate": _rate(decline, ("declined",)),
            "b_overstep_rate": _rate(decline, ("correct", "wrong", "violated")),
            "b_fabricate_rate": _rate(decline, ("violated",)),
            "unknown_verdicts": len([r for r in rows if r["verdict_new"] == "unknown"]),
        }
        for label, name in (("a", "conclude"), ("b", "decline")):
            summary[mode][f"{label}_cases"] = len([r for r in rows if r["expect"] == name])
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="离线重判对照实验结果")
    parser.add_argument("--inputs", default=_DEFAULT_INPUT_GLOB, help="结果 JSON 的 glob")
    parser.add_argument("--llm-config-id", type=int, default=3, help="裁判使用的 LLM 配置 id")
    parser.add_argument("--workers", type=int, default=4, help="裁判并发数")
    parser.add_argument("--resume", help="已有的汇总 JSON：其中非 unknown 的判定会被复用，只补判 unknown")
    parser.add_argument("--out", help="重判后的汇总 JSON 路径")
    args = parser.parse_args(argv)

    paths = sorted(glob.glob(args.inputs))
    if not paths:
        raise SystemExit(f"未找到结果文件：{args.inputs}")

    records: List[Dict[str, Any]] = []
    columns: List[str] = []
    rows = 0
    for path in paths:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        columns = payload.get("columns") or columns
        rows = payload.get("rows") or rows
        # 用例元信息（golden / unsupported）在 ab_compare 的 CASES 里，此处按 id 回填
        for record in payload.get("records", []):
            records.append(record)

    if not records:
        raise SystemExit("结果文件里没有 records")

    # 从 ab_compare 的评测集回填判定要点（golden / unsupported）
    try:
        from app.evaluation.ab_compare import CASES
        meta = {c["id"]: c for c in CASES}
        for record in records:
            case = meta.get(record.get("case_id"), {})
            record.setdefault("golden", case.get("golden", ""))
            record.setdefault("unsupported", case.get("unsupported", ""))
    except Exception:  # noqa: BLE001 —— 回填失败不影响重判，只是提示信息少一些
        pass

    judge_cfg = dict(get_llm_secret(args.llm_config_id))
    judge_cfg["temperature"] = 0
    llm = build_llm(judge_cfg)
    # 裁判调用的默认超时是 20s（生产 LLM 请求预算），对偶发的慢响应太紧，
    # 会造成大量 unknown 并在两种模式间分布不均、把对比带偏。
    # 这里只放宽裁判的超时，不改变被测系统的任何行为。
    try:
        llm.request_timeout = 90.0
    except Exception:  # noqa: BLE001 —— 个别实现不允许改属性时忽略
        pass

    # 复用上一轮已成功的判定，只补判 unknown：裁判调用会偶发超时，
    # 如果每次都全量重判，unknown 会在两种模式间分布不均，把对比结果带偏。
    reuse: Dict[tuple, str] = {}
    if args.resume and Path(args.resume).exists():
        previous = json.loads(Path(args.resume).read_text(encoding="utf-8"))
        for item in previous.get("records", []):
            verdict = item.get("verdict_new")
            if verdict and verdict != "unknown":
                reuse[(item.get("case_id"), item.get("mode"), item.get("repeat"))] = verdict

    def judge_one(record: Dict[str, Any]) -> Dict[str, Any]:
        key = (record.get("case_id"), record.get("mode"), record.get("repeat"))
        if key in reuse:
            return {"verdict": reuse[key], "reason": "复用上一轮判定"}
        return _judge(llm, record, columns, rows)

    todo = len(records) - sum(
        1 for r in records
        if (r.get("case_id"), r.get("mode"), r.get("repeat")) in reuse
    )
    print(f"重判 {len(records)} 条回复（复用 {len(reuse)} 条，需补判 {todo} 条，"
          f"列 {len(columns)} 个，并发 {args.workers}）...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        verdicts = list(pool.map(judge_one, records))
    for record, verdict in zip(records, verdicts):
        record["verdict_old"] = record.get("verdict")
        record["verdict_new"] = verdict["verdict"]
        record["reason_new"] = verdict["reason"]

    summary = aggregate(records)
    report = {
        "columns": columns,
        "rows": rows,
        "case_count": len({r["case_id"] for r in records}),
        "run_count": len(records),
        "inputs": paths,
        "summary": summary,
        "records": records,
    }
    out_path = Path(args.out) if args.out else _BACKEND_DIR / "data" / "eval" / "summary.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 打印关键指标 ──
    print("-" * 92)
    for mode in ("single", "multi"):
        item = summary.get(mode)
        if not item:
            continue
        print(f"【{mode}】")
        print(f"  效率  平均耗时 {item['avg_latency_s']}s   平均 token {item['avg_total_tokens']:,.0f}   "
              f"LLM 调用 {item['avg_llm_calls']}   工具调用 {item['avg_tool_calls']}")
        print(f"  A类({item['a_cases']}例·本该给出结论)  正确 {item['a_correct_rate']:.0%}   "
              f"误拦 {item['a_false_block_rate']:.0%}   答错 {item['a_wrong_rate']:.0%}")
        print(f"  B类({item['b_cases']}例·本该拒绝)     正确拒绝 {item['b_decline_rate']:.0%}   "
              f"越线 {item['b_overstep_rate']:.0%}   编造 {item['b_fabricate_rate']:.0%}")
        if item["unknown_verdicts"]:
            print(f"  （裁判未知判定 {item['unknown_verdicts']} 条）")
    print(f"\n逐例判定差异（旧 -> 新）：")
    changed = [r for r in records if r["verdict_old"] != r["verdict_new"]]
    print(f"  共 {len(changed)} 条因口径修正发生变化")
    for r in changed:
        print(f"  [{r['case_id']:<18}] {r['mode']:<6} {r['verdict_old']} -> {r['verdict_new']}  ({r['reason_new']})")
    print(f"\n完整结果：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
