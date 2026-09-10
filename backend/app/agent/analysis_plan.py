"""分析计划：把自然语言问题先收敛成可展示、可校验的执行计划。"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    id: str
    title: str
    kind: Literal["discover_source", "load_schema", "query", "calculate", "validate", "visualize", "summarize"]
    depends_on: List[str] = Field(default_factory=list)
    status: Literal["pending", "running", "succeeded", "failed", "skipped"] = "pending"
    input: Dict[str, Any] = Field(default_factory=dict)
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    retry_count: int = 0


class AnalysisPlan(BaseModel):
    goal: str
    data_sources: List[str] = Field(default_factory=list)
    metrics: List[Any] = Field(default_factory=list)
    dimensions: List[str] = Field(default_factory=list)
    filters: List[str] = Field(default_factory=list)
    time_range: Optional[str] = None
    grain: Optional[str] = None
    sort: Optional[str] = None
    unit: Optional[str] = None
    steps: List[PlanStep] = Field(default_factory=list)
    output_format: Literal["text", "table", "chart", "report"] = "text"
    assumptions: List[str] = Field(default_factory=list)
    clarification_needed: bool = False
    clarification_question: Optional[str] = None
    version: int = 1
    error: Optional[str] = None
    quality_issues: List[str] = Field(default_factory=list)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)


class AnalysisState(TypedDict):
    question: str
    catalog: List[dict]
    plan: Optional[AnalysisPlan]
    error: Optional[str]


_PLAN_PROMPT = """你是企业数据分析规划器。请只输出 JSON，不要 Markdown。
根据用户问题和候选数据表，生成一个可执行分析计划。
JSON 字段必须是：goal, data_sources, metrics, dimensions, filters, time_range,
grain, sort, unit, steps, output_format, assumptions, clarification_needed,
clarification_question。
steps 中每项必须包含 id、title、kind、depends_on；kind 只能是
discover_source/load_schema/query/calculate/validate/visualize/summarize。
不确定指标口径时不要猜，设置 clarification_needed=true，并提出简短问题。
候选表只是参考，不能编造不存在的表。

候选数据表：{catalog}
用户问题：{question}"""


def _extract_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("规划器未返回 JSON")
    return json.loads(match.group(0))


def _fallback_plan(question: str, catalog: List[dict], error: Optional[str] = None) -> AnalysisPlan:
    """规划失败时仍返回可展示计划，主 Agent 可以沿用现有流程继续处理。"""
    sources = [item["name"] for item in catalog[:3] if item.get("name")]
    return AnalysisPlan(
        goal=question.strip(),
        data_sources=sources,
        steps=[
            PlanStep(id="query_1", title="根据问题查询相关数据", kind="query"),
            PlanStep(id="validate_1", title="检查查询结果", kind="validate", depends_on=["query_1"]),
            PlanStep(id="summarize_1", title="生成分析结论", kind="summarize", depends_on=["validate_1"]),
        ],
        assumptions=["规划器未能生成结构化计划，执行阶段将使用现有 Agent 流程。"],
        error=error,
    )


def make_analysis_plan_graph(llm):
    """构造独立的规划 StateGraph，避免把规划逻辑塞进 API 层。"""
    def plan_node(state: AnalysisState) -> dict:
        prompt = _PLAN_PROMPT.format(
            catalog=json.dumps(state.get("catalog", []), ensure_ascii=False),
            question=state["question"],
        )
        try:
            result = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=state["question"])])
            plan = AnalysisPlan.model_validate(_extract_json(str(result.content)))
            return {"plan": plan, "error": None}
        except Exception as exc:  # noqa: BLE001 - 规划失败必须降级，不阻断原有问答
            return {"plan": _fallback_plan(state["question"], state.get("catalog", []), str(exc)),
                    "error": str(exc)}

    def validate_node(state: AnalysisState) -> dict:
        plan = state["plan"]
        if plan is None or not plan.steps:
            return {"plan": _fallback_plan(state["question"], state.get("catalog", []), "计划没有执行步骤")}
        ids = {step.id for step in plan.steps}
        for step in plan.steps:
            step.depends_on = [dep for dep in step.depends_on if dep in ids and dep != step.id]
        return {"plan": plan}

    graph = StateGraph(AnalysisState)
    graph.add_node("plan", plan_node)
    graph.add_node("validate", validate_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "validate")
    graph.add_edge("validate", END)
    return graph.compile()


def plan_context(plan: AnalysisPlan) -> str:
    """给执行 Agent 的紧凑计划上下文，避免重复注入大对象。"""
    return "【本轮分析计划】\n" + json.dumps(plan.model_dump(exclude_none=True), ensure_ascii=False)


class PlanRuntime:
    """把现有工具节点的执行映射回计划，并产出证据和质量问题。"""

    def __init__(self, plan: AnalysisPlan, question: str = "", on_update=None):
        self.plan = plan
        self.question = question
        self.on_update = on_update
        self._current: Optional[PlanStep] = None
        self._query_seen = False
        self.validation_status = "pending"
        self.validation_report: dict[str, Any] = {}

    def _emit(self):
        if self.on_update:
            self.on_update(self.snapshot())

    def snapshot(self) -> dict:
        return self.plan.model_dump(exclude_none=True)

    def _step_for(self, tool: str) -> Optional[PlanStep]:
        kinds = {
            "list_schemas": "discover_source", "get_schema": "load_schema",
            "get_table_schema": "load_schema", "query_mysql": "query",
            "query_file": "query", "file_stats": "query",
            "compare_periods_tool": "calculate", "compare_groups_tool": "calculate",
            "analyze_trend_tool": "calculate", "detect_outliers_tool": "calculate",
            "analyze_correlation_tool": "calculate",
            "make_chart": "visualize", "generate_chart": "visualize",
            "auto_analyze_and_visualize": "visualize",
        }
        kind = kinds.get(tool)
        if not kind:
            return None
        for step in self.plan.steps:
            if step.status == "pending" and step.kind == kind:
                return step
        for step in self.plan.steps:
            if step.kind == kind and step.status not in ("succeeded", "skipped"):
                return step
        return None

    def before_tool(self, tool: str, args: dict):
        self._current = self._step_for(tool)
        if self._current:
            self._current.status = "running"
            self._current.input = dict(args or {})
            self._emit()

    @staticmethod
    def _table_payload(result: str) -> Optional[dict]:
        match = re.search(r"<!--TABLE:(.*?)-->", result or "", re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except Exception:
            return None

    def after_tool(self, tool: str, args: dict, result: str) -> list[str]:
        issues: List[str] = []
        failed = (
            "错误" in (result or "")
            or "异常" in (result or "")
            or (result or "").startswith("[TOOL_TIMEOUT]")
        )
        payload = self._table_payload(result)
        if tool in ("query_mysql", "query_file"):
            self._query_seen = True
            rows = payload.get("rows", []) if payload else []
            columns = payload.get("columns", []) if payload else []
            sql = (args or {}).get("sql", "")
            if not payload and "空结果" in (result or ""):
                issues.append("查询返回空结果")
            if payload and not rows:
                issues.append("查询返回空结果")
            if len(rows) < 2 and any(word in self.question for word in ("对比", "趋势", "分布", "相关")):
                issues.append("样本量不足，无法支持比较类结论")
            if payload:
                seen = {json.dumps(row, ensure_ascii=False, default=str) for row in rows}
                if len(seen) < len(rows):
                    issues.append("结果中发现重复行，请确认聚合粒度")
                numeric = [float(value) for row in rows for value in row
                           if isinstance(value, (int, float)) and value == value]
                if len(numeric) >= 4:
                    mean = sum(numeric) / len(numeric)
                    variance = sum((value - mean) ** 2 for value in numeric) / len(numeric)
                    deviation = variance ** 0.5
                    if deviation and any(abs(value - mean) > deviation * 3 for value in numeric):
                        issues.append("结果存在明显异常值，请结合业务含义确认")
            if self.plan.filters and "where" not in sql.lower():
                issues.append("查询未体现计划中的过滤条件")
            if self.plan.time_range and "where" not in sql.lower():
                issues.append("查询未体现计划中的时间范围")
            if self.plan.grain and self.plan.dimensions and "group by" not in sql.lower():
                issues.append("查询未体现计划要求的聚合粒度")
            if self.plan.sort and "order by" not in sql.lower():
                issues.append("查询未体现计划要求的排序")
            self.plan.evidence.append({
                "source": tool,
                "sql": sql,
                "columns": columns,
                "row_count": len(rows),
                "quality_issues": issues,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        if "分母为零" in (result or "") or re.search(r"/\s*0(?:\.0+)?\b", result or ""):
            issues.append("检测到分母为零风险")
        if self._current:
            self._current.status = "failed" if failed else "succeeded"
            self._current.output = {"quality_issues": issues}
            self._current.error = (result or "")[:300] if failed else None
            if failed:
                self._current.retry_count += 1
                if self._current.retry_count <= 2:
                    self.plan.version += 1
                    self.plan.assumptions.append("已进入有限修正循环：根据工具错误重新规划当前步骤。")
            self._emit()
        for issue in issues:
            if issue not in self.plan.quality_issues:
                self.plan.quality_issues.append(issue)
        if tool in ("query_mysql", "query_file"):
            self.validation_status = "pending"
        if issues:
            self._emit()
        return issues

    def validate_evidence(self) -> Dict[str, Any]:
        """执行洞察前的确定性门禁，避免验证依赖 LLM 自己宣称通过。"""
        reasons = list(dict.fromkeys(self.plan.quality_issues))
        if not self.plan.evidence:
            reasons.insert(0, "没有真实查询证据")
        if any(step.kind == "query" and step.status == "failed" for step in self.plan.steps):
            reasons.append("数据执行步骤失败")
        reasons = list(dict.fromkeys(reasons))
        approved = not reasons
        self.validation_status = "approved" if approved else "rejected"
        self.validation_report = {
            "status": self.validation_status,
            "evidence_count": len(self.plan.evidence),
            "evidence": list(self.plan.evidence),
            "issues": reasons,
        }
        for step in self.plan.steps:
            if step.kind == "validate" and step.status != "succeeded":
                step.status = "succeeded" if approved else "failed"
                step.output = {"quality_issues": reasons}
                step.error = "；".join(reasons) if reasons else None
        self._emit()
        return dict(self.validation_report)

    def record_worker_failure(self, worker: str, status: str, detail: str = "") -> None:
        """记录 Worker 级失败；超时/异常不能被当成没有问题的空结果。"""
        labels = {
            "timeout": "执行超时",
            "error": "执行异常",
            "busy": "资源繁忙",
        }
        label = labels.get(status, status)
        issue = f"{worker}{label}"
        if detail:
            issue += f"：{detail}"
        if issue not in self.plan.quality_issues:
            self.plan.quality_issues.append(issue)
        if worker in ("statistical_validator", "insight_writer"):
            self.validation_status = "rejected"
        elif worker == "data_executor":
            # 数据执行失败时保持 pending，后续 validate_evidence 会因无证据拒绝。
            self.validation_status = "pending"
        self._emit()

    def finalize(self):
        for step in self.plan.steps:
            if step.status == "pending" and step.kind == "validate":
                step.status = "succeeded" if self.validation_status == "approved" else "failed"
                step.output = {"quality_issues": list(self.plan.quality_issues)}
            if step.status == "pending" and step.kind == "summarize":
                step.status = "succeeded" if self.validation_status == "approved" else "failed"
                step.output = {"quality_issues": list(self.plan.quality_issues)}
        self._emit()
