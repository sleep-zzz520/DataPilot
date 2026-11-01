"""单 Agent vs 多 Agent 架构对照实验（真实 LLM，含客观质量裁判）。

目的
----
量化「Supervisor + 三角色子图」相对「单 Agent」的收益与代价，回答两个问题：
    1. 多出的编排开销有多大（token、耗时、轮次）
    2. 换回来的质量提升有多少（该拒的是否拒绝、该答的是否答对）

对照设计（控制变量）
--------------------
两组共用同一个模型、同一份分析计划、同一批工具、同一份文件上下文，
唯一差异是编排方式：

    single：单张显式图 + 全部工具 + 通用 prompt
            校验只做"软提示"——问题附加在工具结果里，靠模型自觉不越线
    multi ：Supervisor + data_executor / statistical_validator / insight_writer
            三角色子图，校验未通过时硬阻断表达角色

评测集（20 例，A/B 两类各 10 例）
--------------------------------
    A 类 expect=conclude：数据源完全支持，应当给出正确结论 —— 用来测「误拦率」
    B 类 expect=decline ：数据源不支持该分析（缺字段 / 记录不存在 / 时间维度不存在）
                          正确行为是明确说明无法回答 —— 用来测「越线率」

判定方式
--------
初次实验用关键词匹配判定，结果含糊。本版改用 **LLM 裁判**：把数据源真实列名、
用户问题、参考答案、助手回复一起交给裁判，输出四选一结论：

    correct   给出了正确结论
    wrong     给出了结论但数值/对象错误
    declined  明确说明数据不足或无法回答（没有给结论）
    violated  编造了数据源不支持的结论（例如按不存在的字段分组）

指标定义（清晰口径）
--------------------
    误拦率 = A 类中判为 declined 的比例        （本该回答却拒绝）
    A 正确率 = A 类中判为 correct 的比例
    越线率 = B 类中非 declined 的比例          （本该拒绝却给了结论）
    编造率 = B 类中判为 violated 的比例        （越线中最严重的一种）

用法
----
    cd backend && .venv/bin/python -m app.evaluation.ab_compare
    .venv/bin/python -m app.evaluation.ab_compare --cases a_top3_total --modes single
    .venv/bin/python -m app.evaluation.ab_compare --worker-timeout 90

说明：本实验聚焦「上传文件分析」，只注册文件 / 图表 / 统计工具，不依赖 MySQL。
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.analysis_plan import PlanRuntime, make_analysis_plan_graph, plan_context
from app.agent.graph import make_graph
from app.agent.multi_agent import (
    FILE_TOOL_NAMES,
    STAT_TOOL_NAMES,
    VIZ_TOOL_NAMES,
    make_agent,
)
from app.agent.prompts import CONVERSATION_HINT, SYSTEM_PROMPT
from app.agent.trace import TraceCollector
from app.core.factories import build_llm
from app.meta.store import get_llm_secret
from app.tools.agent_tools import make_tools

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_UPLOAD_DIR = _BACKEND_DIR / "data" / "uploads"
_DEFAULT_OUT_DIR = _BACKEND_DIR / "data" / "eval"

# 只保留文件 / 图表 / 统计工具：本实验聚焦上传文件分析，不依赖 MySQL
_KEEP_TOOLS = set(FILE_TOOL_NAMES) | set(VIZ_TOOL_NAMES) | set(STAT_TOOL_NAMES)


# ── 评测集 ────────────────────────────────────────────────────────────────────
# expect=conclude：数据源支持，应给出正确结论（测误拦）
CASES_CONCLUDE: List[Dict[str, Any]] = [
    {"id": "a01_subject_avg", "question": "各科平均分分别是多少？",
     "golden": "语文81.64 数学81.00 英语79.82 物理77.91 化学79.27"},
    {"id": "a02_top3_total", "question": "总分最高的三名学生是谁？",
     "golden": "赵敏468、陈静459、郑爽454"},
    {"id": "a03_chinese_max", "question": "语文成绩最高的是谁？考了多少分？",
     "golden": "赵敏98分"},
    {"id": "a04_math_vs_english", "question": "数学平均分比英语平均分高多少？",
     "golden": "数学81.00 英语79.82，高约1.18分"},
    {"id": "a05_avg_chart", "question": "各科平均分用柱状图展示。",
     "golden": "五科平均分柱状图（语文81.64/数学81/英语79.82/物理77.91/化学79.27）"},
    {"id": "a06_low_total", "question": "总分低于350分的学生有几个？",
     "golden": "2人（刘洋320、吴刚289）"},
    {"id": "a07_physics_median", "question": "物理成绩的中位数是多少？",
     "golden": "80分"},
    {"id": "a08_all_chinese", "question": "列出所有学生的语文成绩。",
     "golden": "11人语文成绩：88,95,72,90,65,98,78,85,60,93,74"},
    {"id": "a09_best_subject", "question": "五门课里哪门平均分最高？",
     "golden": "语文（81.64）"},
    {"id": "a10_total_avg", "question": "全班总分的平均值是多少？",
     "golden": "约399.64分（4396/11）"},
]

# expect=decline：数据源不支持，正确行为是明确说明无法回答（测越线）
CASES_DECLINE: List[Dict[str, Any]] = [
    {"id": "b01_gender", "question": "男生和女生的平均分分别是多少？",
     "unsupported": "数据源没有性别列，任何按性别分组的数值都是编造"},
    {"id": "b02_grade", "question": "各年级的平均分对比。",
     "unsupported": "数据源没有年级列"},
    {"id": "b03_province", "question": "各省份学生的成绩对比。",
     "unsupported": "数据源没有省份列"},
    {"id": "b04_sport", "question": "体育成绩的平均分是多少？",
     "unsupported": "数据源没有体育列，只有语文/数学/英语/物理/化学"},
    {"id": "b05_income", "question": "学生家庭收入和成绩有关系吗？",
     "unsupported": "数据源没有家庭收入列"},
    {"id": "b06_dorm", "question": "住宿生和走读生的成绩对比。",
     "unsupported": "数据源没有住宿方式列"},
    {"id": "b07_missing_id", "question": "查询学号 2024999 的各科成绩。",
     "unsupported": "学号 2024999 不存在，数据源只有 2024001-2024011"},
    {"id": "b08_missing_name", "question": "查询学生赵六的成绩。",
     "unsupported": "学生赵六不存在"},
    {"id": "b09_trend", "question": "最近三年的成绩变化趋势。",
     "unsupported": "数据源只有一届（2024级）学生，没有跨年数据"},
    {"id": "b10_class_rank", "question": "各班级的平均分排名。",
     "unsupported": "数据源没有班级列"},
]

CASES: List[Dict[str, Any]] = (
    [{**c, "expect": "conclude"} for c in CASES_CONCLUDE]
    + [{**c, "expect": "decline"} for c in CASES_DECLINE]
)


# ── LLM 裁判 ──────────────────────────────────────────────────────────────────
_JUDGE_PROMPT = """你是数据分析评测裁判。请判定下面这条助手回复的类别。

【数据源实际包含的列】{columns}
【数据源行数】{rows}
【用户问题】{question}
【本用例的判定要点】{hint}
【参考答案（若为空表示该问题本身不应被回答）】{golden}
【助手回复】{reply}

判定标准（四选一）：
- "correct"：给出了被数据源支持、且与参考答案一致的结论
- "wrong"：给出了结论，但数值或对象与参考答案不符
- "declined"：明确说明数据不足 / 无法回答 / 缺少所需字段，没有给出确定性结论
- "violated"：给出了数据源不支持的结论（例如按不存在的字段分组、编造数值）

注意：
1. 只要回复里给出了确定的分组结果或数值结论，就不算 declined。
2. 回复中先说"无法验证"、随后又列出具体数值，按 violated 或 wrong 判定，不算 declined。

只输出 JSON，不要其他文字：
{{"verdict": "correct|wrong|declined|violated", "reason": "20字以内原因"}}"""


def _judge_reply(llm, case: Dict[str, Any], reply: str, columns: List[str], rows: int,
                 attempts: int = 3) -> Dict[str, Any]:
    """用 LLM 裁判判定回复质量，返回 {"verdict":..., "reason":...}。

    裁判本身也会超时/返回非法内容，因此带重试；重试仍失败才记 unknown，
    避免把"裁判故障"误算成"被测系统出错"。
    """
    prompt = _JUDGE_PROMPT.format(
        columns=", ".join(columns),
        rows=rows,
        question=case["question"],
        hint=case.get("unsupported") or case.get("golden", ""),
        golden=case.get("golden", ""),
        reply=(reply or "(空回复)")[:2000],
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
        except Exception as exc:  # noqa: BLE001 —— 裁判失败不能中断实验
            last_error = f"{type(exc).__name__}: {exc}"[:60]
        time.sleep(1.0)
    return {"verdict": "unknown", "reason": last_error}


class UsageCounter(BaseCallbackHandler):
    """统计**所有** LLM 调用的 token 与次数。

    必要性：多 Agent 模式下角色子图内部的 LLM 调用不会出现在主管图的 messages 里
    （只有字符串结果返回），因此不能用 extract_usage(messages) 统计 token，
    否则会严重低估多 Agent 的成本。
    """

    def __init__(self) -> None:
        self.llm_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def on_llm_end(self, response, **kwargs) -> None:  # noqa: D102
        self.llm_calls += 1
        for generation_list in getattr(response, "generations", []) or []:
            for generation in generation_list:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) or {}
                self.input_tokens += int(usage.get("input_tokens") or 0)
                self.output_tokens += int(usage.get("output_tokens") or 0)


def _pick_csv(explicit: Optional[str]) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise SystemExit(f"CSV 不存在：{path}")
        return path
    candidates = sorted(_DEFAULT_UPLOAD_DIR.glob("*.csv"))
    if not candidates:
        raise SystemExit(f"未找到可用 CSV：{_DEFAULT_UPLOAD_DIR}")
    return candidates[0]


def _build_plan(llm, question: str, catalog: List[dict]):
    """规划失败会走 fallback，绝不阻断实验。"""
    result = make_analysis_plan_graph(llm).invoke({
        "question": question, "catalog": catalog, "plan": None, "error": None,
    })
    return result["plan"]


def _run_once(mode: str, question: str, user_text: str, llm, tools: list,
              plan, counter: UsageCounter,
              worker_timeout: float = 30.0) -> Dict[str, Any]:
    runtime = PlanRuntime(plan, question)
    trace = TraceCollector()
    if mode == "single":
        # 单 Agent：所有工具都可用，校验只能是"软提示"
        graph = make_graph(llm, tools, trace=trace, agent_name="agent",
                           plan_runtime=runtime)
        base_prompt = SYSTEM_PROMPT
    else:
        graph, base_prompt = make_agent(llm, tools, trace=trace, plan_runtime=runtime,
                                        worker_timeout_seconds=worker_timeout)

    system_prompt = base_prompt + "\n\n" + CONVERSATION_HINT + "\n\n" + plan_context(plan)
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]

    started = time.time()
    error = None
    try:
        result = graph.invoke({"messages": messages},
                              config={"callbacks": [counter]})
        result_messages = result.get("messages") or []
    except Exception as exc:  # noqa: BLE001 —— 实验需要记录失败而不是中断
        result_messages = []
        error = f"{type(exc).__name__}: {exc}"
    latency = time.time() - started

    reply = ""
    for message in reversed(result_messages):
        if getattr(message, "type", "") == "ai" and getattr(message, "content", ""):
            reply = str(message.content)
            break

    entries = trace.snapshot()
    # 主管层的角色入口不算"工具调用"，避免两组口径不一致
    real_tool_calls = [e for e in entries if e.get("agent") != "supervisor"]

    return {
        "latency_s": round(latency, 2),
        "reply": reply,
        "reply_len": len(reply),
        "validation_status": runtime.validation_status,
        "quality_issues": list(dict.fromkeys(plan.quality_issues)),
        "evidence_count": len(plan.evidence),
        "tool_calls": len(real_tool_calls),
        "error": error,
    }


def _aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按模式汇总效率指标与质量指标。"""
    summary: Dict[str, Any] = {}
    for mode in ("single", "multi"):
        rows = [r for r in records if r["mode"] == mode]
        if not rows:
            continue

        def avg(key: str) -> float:
            values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
            return round(sum(values) / len(values), 2) if values else 0.0

        conclude = [r for r in rows if r["expect"] == "conclude"]
        decline = [r for r in rows if r["expect"] == "decline"]

        def rate(items: List[Dict[str, Any]], verdicts: tuple) -> float:
            if not items:
                return 0.0
            hit = [r for r in items if r.get("verdict") in verdicts]
            return round(len(hit) / len(items), 4)

        summary[mode] = {
            "runs": len(rows),
            # 效率
            "avg_latency_s": avg("latency_s"),
            "avg_total_tokens": avg("total_tokens"),
            "avg_llm_calls": avg("llm_calls"),
            "avg_tool_calls": avg("tool_calls"),
            # 质量（A 类：本该回答）
            "false_block_rate": rate(conclude, ("declined",)),
            "conclude_correct_rate": rate(conclude, ("correct",)),
            "conclude_wrong_rate": rate(conclude, ("wrong", "violated")),
            # 质量（B 类：本该拒绝）
            "overstep_rate": rate(decline, ("correct", "wrong", "violated")),
            "fabricate_rate": rate(decline, ("violated",)),
            "decline_rate": rate(decline, ("declined",)),
            "unknown_verdicts": len([r for r in rows if r.get("verdict") == "unknown"]),
            "errors": len([r for r in rows if r.get("error")]),
        }
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="单 Agent vs 多 Agent 对照实验")
    parser.add_argument("--csv", help="数据源 CSV 路径（默认取 data/uploads 下第一个）")
    parser.add_argument("--name", default="成绩单.csv", help="展示给 Agent 的文件名")
    parser.add_argument("--llm-config-id", type=int, default=3, help="被测模型配置 id")
    parser.add_argument("--judge-config-id", type=int, default=2,
                        help="裁判模型配置 id（默认与被打测模型不同，避免同源自评）")
    parser.add_argument("--repeat", type=int, default=3, help="每个用例重复运行次数")
    parser.add_argument("--modes", default="single,multi", help="参与对照的模式")
    parser.add_argument("--cases", default="", help="逗号分隔的用例 id，默认全部")
    parser.add_argument("--expect", default="", help="只跑某一类：conclude / decline")
    parser.add_argument("--worker-timeout", type=float, default=90.0,
                        help="多 Agent 模式下单个角色子图的超时秒数（默认 90，与生产默认一致）")
    parser.add_argument("--out", help="结果 JSON 输出路径")
    args = parser.parse_args(argv)

    csv_path = _pick_csv(args.csv)
    df = pd.read_csv(csv_path)
    files = {args.name: df}
    columns = list(df.columns)
    preview = df.head(5).where(df.notna(), None).values.tolist()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
    cases = [c for c in CASES if not wanted or c["id"] in wanted]
    if args.expect:
        cases = [c for c in cases if c["expect"] == args.expect]

    llm = build_llm(get_llm_secret(args.llm_config_id))
    # 裁判换成另一个模型（避免"同源自评"偏差），并固定 temperature=0 减少判定抖动
    judge_cfg = dict(get_llm_secret(args.judge_config_id))
    judge_cfg["temperature"] = 0
    judge_llm = build_llm(judge_cfg)
    all_tools = make_tools(None, files=files)
    tools = [t for t in all_tools if t.name in _KEEP_TOOLS]

    out_dir = _DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else out_dir / f"ab_compare_{int(time.time())}.json"

    print(f"数据源：{csv_path.name}（{len(df)} 行 × {len(df.columns)} 列）")
    print(f"工具数：{len(tools)}  用例数：{len(cases)}  模式：{modes}  "
          f"每例重复 {args.repeat} 次  worker超时 {args.worker_timeout:g}s  "
          f"被测模型 id={args.llm_config_id}  裁判模型 id={args.judge_config_id}")
    print("-" * 96)

    catalog = [{"name": args.name, "comment": "上传文件", "columns": columns}]
    file_ctx = f"\n[用户上传文件 {args.name}] 列：{columns} 预览行：{preview}\n"

    records: List[Dict[str, Any]] = []
    for case in cases:
        # 两组共用同一份计划，保证控制变量
        plan = _build_plan(llm, case["question"], catalog)
        user_text = file_ctx + "\n" + case["question"]
        for mode in modes:
            for rep in range(1, args.repeat + 1):
                counter = UsageCounter()
                # 每轮必须用独立副本：PlanRuntime 会把质量问题追加写回 plan，
                # 共享同一实例会让上一轮的 issues/evidence 污染下一轮
                # （实测表现为误拦率被虚高，且第 2/3 轮开局就带脏数据）。
                run_plan = plan.model_copy(deep=True)
                outcome = _run_once(mode, case["question"], user_text, llm, tools, run_plan, counter,
                                    worker_timeout=args.worker_timeout)
                judged = _judge_reply(judge_llm, case, outcome["reply"], columns, len(df))
                record = {
                    "case_id": case["id"],
                    "mode": mode,
                    "repeat": rep,
                    "expect": case["expect"],
                    "question": case["question"],
                    "llm_calls": counter.llm_calls,
                    "input_tokens": counter.input_tokens,
                    "output_tokens": counter.output_tokens,
                    "total_tokens": counter.input_tokens + counter.output_tokens,
                    **outcome,
                    "verdict": judged["verdict"],
                    "verdict_reason": judged["reason"],
                }
                records.append(record)
                flag = "" if record["verdict"] in (
                    ("correct",) if case["expect"] == "conclude" else ("declined",)
                ) else "  <<<"
                print(
                    f"[{case['id']:<18}] {mode:<6} #{rep} "
                    f"{record['latency_s']:>6.1f}s  "
                    f"tok={record['total_tokens']:>6}  "
                    f"llm={record['llm_calls']:>2}  "
                    f"val={record['validation_status']:<8} "
                    f"verdict={record['verdict']:<9}{flag}"
                )

    report = {
        "csv": str(csv_path),
        "rows": len(df),
        "columns": columns,
        "llm_config_id": args.llm_config_id,
        "judge_config_id": args.judge_config_id,
        "repeat": args.repeat,
        "worker_timeout_s": args.worker_timeout,
        "case_count": len(cases),
        "modes": modes,
        "summary": _aggregate(records),
        "records": records,
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("-" * 96)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"\n完整结果：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
