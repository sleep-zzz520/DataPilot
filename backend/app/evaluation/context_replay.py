"""上下文裁剪确定性同输入回放，不调用模型，不评估回答质量或真实费用。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.memory import store

DEFAULT_BASELINE = Path(__file__).resolve().parents[2] / "data/eval/20261031/baseline_sources/memory/store.py"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "data/eval/20261031/context-replay.json"
BUDGETS = (-20, 0, 1, 32, 64, 128, 256, 512, 2048, 8192)


@dataclass(frozen=True)
class ReplayScenario:
    name: str
    messages: list
    description: str


def _call(identifier: str, argument: str = "SELECT 1") -> dict:
    return {"id": identifier, "name": "query_mysql", "args": {"sql": argument}}


def replay_scenarios() -> list[ReplayScenario]:
    """覆盖协议完整/中断、空content大参数、超大末条与长混合语言历史。"""
    scenarios = [
        ReplayScenario("parallel_tools", [HumanMessage(content="比较两库订单"),
            AIMessage(content="", tool_calls=[_call("p1"), _call("p2")]),
            ToolMessage(content="第一库120单；" * 20, tool_call_id="p1"),
            ToolMessage(content="第二库80单", tool_call_id="p2"),
            AIMessage(content="两库合计200单")], "并行调用及全部响应必须整体保留或移除"),
        ReplayScenario("sequential_tools", [HumanMessage(content="校验订单趋势"),
            AIMessage(content="", tool_calls=[_call("s1")]),
            ToolMessage(content="订单120", tool_call_id="s1"),
            AIMessage(content="", tool_calls=[_call("s2")]),
            ToolMessage(content="校验通过", tool_call_id="s2"),
            AIMessage(content="已完成")], "连续工具轮需要维护各调用与响应配对"),
        ReplayScenario("orphan_tool_input", [ToolMessage(content="中断遗留响应", tool_call_id="absent"),
            HumanMessage(content="继续分析"), AIMessage(content="请提供当前数据")], "恢复的遗留孤立响应应移除"),
        ReplayScenario("incomplete_parallel_input", [HumanMessage(content="查询两个数据源"),
            AIMessage(content="", tool_calls=[_call("i1"), _call("i2")]),
            ToolMessage(content="只有第一个结果", tool_call_id="i1"),
            HumanMessage(content="上轮中断，请继续")], "未完成的并行工具请求不应重放"),
        ReplayScenario("large_empty_content_tool_args", [HumanMessage(content="查询明细"),
            AIMessage(content="", tool_calls=[_call("large", "SELECT " + "field," * 4000)]),
            ToolMessage(content="[]", tool_call_id="large"),
            AIMessage(content="没有记录")], "空content的超大工具参数仍需计入预算"),
        ReplayScenario("oversized_last_message", [HumanMessage(content="简短问题"),
            AIMessage(content="超大输出" * 5000)], "超限最后一条历史不能越过预算强留"),
        ReplayScenario("mixed_language_long_history", [message for index in range(80)
            for message in (HumanMessage(content=f"第{index}轮：按地区统计 revenue / order_count，保留CNY单位"),
                            AIMessage(content=f"round={index}; 上海100单，北京80单。Current source is sales_2026."))],
            "中英文长历史按照统一估算器核算"),
        ReplayScenario("many_short_messages", [message for index in range(100)
            for message in (HumanMessage(content=f"q{index}"), AIMessage(content="ok"))],
            "短消息角色封装开销和历史保留"),
        ReplayScenario("large_tool_result", [HumanMessage(content="检查查询输出"),
            AIMessage(content="", tool_calls=[_call("result")]),
            ToolMessage(content="行结果12345\n" * 1200, tool_call_id="result"),
            AIMessage(content="结果已校验，后续需聚合")], "大工具响应与调用必须一起裁剪"),
    ]
    for scenario in scenarios:
        for index, message in enumerate(scenario.messages):
            message.id = f"{scenario.name}:{index}"
    return scenarios


def protocol_errors(messages: list) -> list[str]:
    """独立协议oracle，不复用被测实现的分组或裁剪函数。"""
    errors = []
    pending: set[str] = set()
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage):
            identifier = str(message.tool_call_id)
            if identifier not in pending:
                errors.append(f"orphan_or_duplicate_response:{index}:{identifier}")
            else:
                pending.remove(identifier)
            continue
        if pending:
            errors.append(f"missing_responses_before:{index}:{','.join(sorted(pending))}")
            pending.clear()
        if isinstance(message, AIMessage) and message.tool_calls:
            identifiers = [str(call.get("id") or "") for call in message.tool_calls]
            if any(not identifier for identifier in identifiers) or len(identifiers) != len(set(identifiers)):
                errors.append(f"invalid_request_ids:{index}")
            pending.update(identifiers)
    if pending:
        errors.append("missing_final_responses:" + ",".join(sorted(pending)))
    return errors


def _load_baseline(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"原版上下文模块不存在：{path}；必须先备份原文件才能回放")
    spec = importlib.util.spec_from_file_location("context_replay_original_store", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载原版模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _measure(function, scenario: ReplayScenario, budget: int) -> dict:
    original = list(scenario.messages)
    snapshot = [message.model_dump() for message in original]
    try:
        retained = function(list(original), budget)
        errors = protocol_errors(retained)
        cost = store.estimate_messages_tokens(retained)
        last = original[-1]
        # 只检查可独立保留、预算装得下的最近普通消息；超大末条不作保留要求。
        eligible = (not isinstance(last, ToolMessage) and not getattr(last, "tool_calls", None)
                    and store.estimate_message_tokens(last) <= max(0, budget))
        return {
            "error": None,
            "protocol_valid": not errors,
            "protocol_errors": errors,
            "within_estimated_budget": cost <= max(0, budget),
            "estimated_tokens": cost,
            "retained_messages": len(retained),
            "retained_human_messages": sum(isinstance(message, HumanMessage) for message in retained),
            "retained_tool_messages": sum(isinstance(message, ToolMessage) for message in retained),
            "retained_message_ids": [message.id for message in retained],
            "latest_retention_eligible": eligible,
            "latest_message_retained": bool(retained and retained[-1] is last),
            "input_unchanged": snapshot == [message.model_dump() for message in scenario.messages],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "protocol_valid": False,
                "protocol_errors": [], "within_estimated_budget": False, "estimated_tokens": None,
                "retained_messages": 0, "retained_human_messages": 0, "retained_tool_messages": 0,
                "retained_message_ids": [], "latest_retention_eligible": False,
                "latest_message_retained": False, "input_unchanged": snapshot == [message.model_dump() for message in scenario.messages]}


def _aggregate(results: list[dict]) -> dict:
    count = len(results)
    return {
        "case_count": count,
        "successful_execution_count": sum(result["error"] is None for result in results),
        "protocol_valid_count": sum(result["protocol_valid"] for result in results),
        "within_estimated_budget_count": sum(result["within_estimated_budget"] for result in results),
        "protocol_and_budget_valid_count": sum(result["protocol_valid"] and result["within_estimated_budget"] for result in results),
        "protocol_valid_rate": sum(result["protocol_valid"] for result in results) / count if count else 0,
        "within_estimated_budget_rate": sum(result["within_estimated_budget"] for result in results) / count if count else 0,
        "retained_messages_total": sum(result["retained_messages"] for result in results),
        "retained_tool_messages_total": sum(result["retained_tool_messages"] for result in results),
        "latest_retention_eligible_count": sum(result["latest_retention_eligible"] for result in results),
        "latest_retention_success_count": sum(result["latest_retention_eligible"] and result["latest_message_retained"] for result in results),
        "input_unchanged_count": sum(result["input_unchanged"] for result in results),
    }


def run_replay(baseline_path: Path = DEFAULT_BASELINE, budgets=BUDGETS) -> dict:
    baseline_path = Path(baseline_path)
    baseline = _load_baseline(baseline_path)
    scenarios = replay_scenarios()
    cases = []
    for scenario in scenarios:
        for budget in budgets:
            cases.append({
                "id": f"{scenario.name}/budget={budget}",
                "scenario": scenario.name, "description": scenario.description,
                "input_protocol_valid": not protocol_errors(scenario.messages),
                "budget": budget, "effective_budget": max(0, budget),
                "input_messages": len(scenario.messages),
                "input_estimated_tokens": store.estimate_messages_tokens(scenario.messages),
                "before": _measure(baseline.budget_history, scenario, budget),
                "after": _measure(store.budget_history, scenario, budget),
            })
    summary = {mode: _aggregate([case[mode] for case in cases]) for mode in ("before", "after")}
    by_scenario = {scenario.name: {mode: _aggregate([case[mode] for case in cases if case["scenario"] == scenario.name])
                                  for mode in ("before", "after")} for scenario in scenarios}
    by_input_protocol = {
        name: {mode: _aggregate([case[mode] for case in cases if case["input_protocol_valid"] == valid])
               for mode in ("before", "after")}
        for name, valid in (("valid_input", True), ("incomplete_legacy_input", False))
    }
    return {
        "schema_version": 1, "evaluation": "deterministic_context_history_replay",
        "llm_calls": 0,
        "oracle": "current app.memory.store.estimate_messages_tokens; nonpositive budget means zero capacity",
        "limitations": ["token为统一近似估算，不是供应商精确token计量", "不测量模型回答质量、任务成功率或实际费用",
                        "孤立响应和中断调用场景用于测试遗留历史修复，其输入本身协议不完整"],
        "baseline_source": str(baseline_path),
        "baseline_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        "current_sha256": hashlib.sha256(Path(store.__file__).read_bytes()).hexdigest(),
        "scenario_count": len(scenarios), "budgets": list(budgets), "case_count": len(cases),
        "summary": summary, "by_scenario": by_scenario, "by_input_protocol": by_input_protocol, "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_replay(args.baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "case_count": result["case_count"],
                      "summary": result["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
