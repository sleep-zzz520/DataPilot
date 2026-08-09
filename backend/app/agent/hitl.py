"""有限 Human-in-the-loop：只拦截少数高风险分析，不引入人工工单状态机。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


_SENSITIVE = re.compile(
    r"姓名|手机号|手机号码|电话|邮箱|email|身份证|住址|地址|银行卡|工资|薪资|收入|密码|密钥|token|secret|客户联系方式",
    re.I,
)
_SENSITIVE_SOURCE = re.compile(r"user|customer|employee|payroll|salary|contact|个人|客户|员工", re.I)
_EXPORT = re.compile(r"导出|下载|export|download|csv|xlsx|excel|pdf|报表", re.I)
_WIDE_QUERY = re.compile(r"全量|全部|所有|明细|历史|长期|跨年|大表|不限制|从开始|至今|最近三年|最近5年", re.I)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{_text(k)} {_text(v)}" for k, v in value.items())
    return str(value)


def assess_confirmation(question: str, plan, file_metadata: Optional[List[Dict[str, Any]]] = None) -> dict:
    """根据计划和请求做确定性风险判断，返回前端可直接展示的确认摘要。"""
    plan_text = _text(plan.model_dump(exclude_none=True))
    file_columns = _text([item.get("columns", []) for item in (file_metadata or [])])
    all_text = f"{question} {plan_text} {file_columns}"
    reasons: list[str] = []
    if plan.clarification_needed:
        reasons.append("metric_ambiguity")

    query_count = sum(step.kind == "query" for step in plan.steps)
    high_cost = (
        query_count >= 2 or len(plan.steps) >= 6 or _WIDE_QUERY.search(all_text)
        or plan.output_format == "report"
    )
    if high_cost:
        reasons.append("high_cost")
    if _EXPORT.search(question) or plan.output_format == "report":
        reasons.append("data_export")
    if _SENSITIVE.search(all_text) or _SENSITIVE_SOURCE.search(all_text):
        reasons.append("sensitive_data")

    if not reasons:
        return {"required": False}

    reason_labels = {
        "high_cost": "可能触发较多模型调用或大范围查询",
        "sensitive_data": "请求涉及可能包含个人或敏感字段的数据",
        "data_export": "请求包含导出、下载或报告产物",
        "metric_ambiguity": "指标口径尚未明确，直接执行可能得到错误结论",
    }
    query_steps = max(1, query_count)
    token_min = 1200 + len(plan.steps) * 180 + query_steps * 300
    token_max = token_min + query_steps * 1400
    scope = list(dict.fromkeys(
        list(plan.data_sources)
        + [f"指标：{_text(item)}" for item in plan.metrics]
        + [f"维度：{item}" for item in plan.dimensions]
        + ([f"时间：{plan.time_range}"] if plan.time_range else [])
        + ([f"过滤：{item}" for item in plan.filters])
    ))
    steps = [step.title for step in plan.steps[:8]]
    return {
        "required": True,
        "mode": "clarification" if "metric_ambiguity" in reasons else "approval",
        "reasons": reasons,
        "reason_labels": [reason_labels[item] for item in reasons],
        "estimated_cost": {
            "model_calls": f"至少 {2 + query_steps} 次",
            "token_range": f"约 {token_min:,}–{token_max:,} tokens",
            "note": "实际费用取决于当前模型配置，完成后以用量记录为准。",
        },
        "data_scope": scope or ["按分析计划选择相关数据源"],
        "risks": [reason_labels[item] for item in reasons],
        "steps": steps or ["生成计划", "执行查询", "校验并总结"],
        "prompt": plan.clarification_question if plan.clarification_needed else "确认后才会执行上述分析。",
    }
