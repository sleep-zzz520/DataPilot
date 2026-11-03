"""分析计划：把自然语言问题先收敛成可展示、可校验的执行计划。"""
from __future__ import annotations

import json
import hashlib
from copy import deepcopy
import re
import time
from typing import Any, Dict, List, Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, field_validator


# ── 模型输出归一化 ────────────────────────────────────────────────────────────
# 背景（实测踩坑）：LLM 输出的 JSON 结构与 schema 常有偏差——sort 给成对象数组、
# steps.id 给成数字、output_format 给成中文"表格"。Pydantic 严格校验会直接拒绝，
# 导致 plan_node 抛异常 → 整个规划降级为 fallback（dimensions 为空），
# 于是所有依赖计划的校验（维度存在性、口径核对）全部失效。
# 这里在 before 阶段做归一化，让规划真正跑起来。
_STEP_KIND_ALIASES = {
    "discover_source": "discover_source", "发现数据源": "discover_source", "发现": "discover_source",
    "load_schema": "load_schema", "加载表结构": "load_schema", "读取表结构": "load_schema",
    "查看表结构": "load_schema",
    "query": "query", "查询": "query", "取数": "query", "查询数据": "query",
    "calculate": "calculate", "计算": "calculate", "统计": "calculate", "统计计算": "calculate",
    "validate": "validate", "验证": "validate", "校验": "validate", "检查": "validate",
    "visualize": "visualize", "可视化": "visualize", "图表": "visualize", "画图": "visualize",
    "summarize": "summarize", "总结": "summarize", "汇总": "summarize", "结论": "summarize",
}
_OUTPUT_FORMAT_ALIASES = (
    ("chart", ("chart", "图表", "图", "可视化", "柱状", "折线", "饼图")),
    ("report", ("report", "报告", "报表")),
    ("table", ("table", "表格", "表")),
    ("text", ("text", "文本", "文字", "纯文本")),
)


def _as_text(value: Any) -> str:
    """把任意结构归一化成字符串（模型常把 str 字段写成 list/dict/int）。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _as_text_list(value: Any) -> List[str]:
    """把任意结构归一化成字符串列表（模型常把 List[str] 写成 str 或对象数组）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        items = [_as_text(item) for item in value]
        return [item for item in items if item.strip()]
    return [_as_text(value)]


def _norm_step_kind(value: Any) -> str:
    text = _as_text(value).strip().lower()
    return _STEP_KIND_ALIASES.get(text, "query")


def _norm_output_format(value: Any) -> str:
    text = _as_text(value).strip().lower()
    for canonical, aliases in _OUTPUT_FORMAT_ALIASES:
        if any(alias in text for alias in aliases):
            return canonical
    return "text"


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
    evidence_ids: List[str] = Field(default_factory=list)

    @field_validator("id", "title", mode="before")
    @classmethod
    def _coerce_text(cls, value: Any) -> str:
        return _as_text(value)

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, value: Any) -> str:
        return _norm_step_kind(value)

    @field_validator("depends_on", mode="before")
    @classmethod
    def _coerce_depends_on(cls, value: Any) -> List[str]:
        return _as_text_list(value)


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
    result_columns: List[str] = Field(default_factory=list)
    output_format: Literal["text", "table", "chart", "report"] = "text"
    assumptions: List[str] = Field(default_factory=list)
    clarification_needed: bool = False
    clarification_question: Optional[str] = None
    version: int = 1
    error: Optional[str] = None
    quality_issues: List[str] = Field(default_factory=list)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    bound_contracts: List[Dict[str, Any]] = Field(default_factory=list)
    active_issues: List[Dict[str, Any]] = Field(default_factory=list)
    issue_history: List[Dict[str, Any]] = Field(default_factory=list)
    repair_history: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("goal", mode="before")
    @classmethod
    def _coerce_goal(cls, value: Any) -> str:
        return _as_text(value)

    @field_validator("data_sources", "dimensions", "filters", "assumptions", mode="before")
    @classmethod
    def _coerce_text_lists(cls, value: Any) -> List[str]:
        return _as_text_list(value)

    @field_validator("time_range", "grain", "sort", "unit", "clarification_question", mode="before")
    @classmethod
    def _coerce_optional_text(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        return _as_text(value) or None

    @field_validator("sort", "time_range", mode="after")
    @classmethod
    def _normalize_unconstrained_fields(cls, value: Optional[str], info) -> Optional[str]:
        if value is None or not value.strip():
            return None
        text = value.strip().casefold()
        if text in {"无", "none"}:
            return None
        if info.field_name == "sort" and text in {"不排序", "无需排序"}:
            return None
        if info.field_name == "time_range" and text in {"全部数据", "全时段", "不限时间"}:
            return None
        try:
            structured = json.loads(value)
        except ValueError:
            return value
        if structured in (None, [], {}):
            return None
        if (info.field_name == "time_range" and isinstance(structured, dict)
                and structured.get("type") in (None, "all")
                and structured.keys() <= {"type", "start", "end", "description"}
                and structured.get("description") in (None, "", "未指定时间范围，统计全部数据")
                and structured.get("start") is None and structured.get("end") is None):
            return None
        return value

    @field_validator("result_columns", mode="before")
    @classmethod
    def _coerce_result_columns(cls, value: Any) -> List[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            return []
        return list(dict.fromkeys(item.strip() for item in value))

    @field_validator("output_format", mode="before")
    @classmethod
    def _coerce_output_format(cls, value: Any) -> str:
        return _norm_output_format(value)

    @field_validator("metrics", mode="before")
    @classmethod
    def _coerce_metrics(cls, value: Any) -> List[Any]:
        if value is None:
            return []
        return list(value) if isinstance(value, (list, tuple, set)) else [value]

    @field_validator("steps", mode="before")
    @classmethod
    def _coerce_steps(cls, value: Any) -> List[Any]:
        if value is None:
            return []
        return list(value) if isinstance(value, (list, tuple)) else [value]


class AnalysisState(TypedDict):
    question: str
    catalog: List[dict]
    plan: Optional[AnalysisPlan]
    error: Optional[str]
    memory_context: str


_PLAN_PROMPT = """你是企业数据分析规划器。请只输出 JSON，不要 Markdown。
根据用户问题和候选数据表，生成一个可执行分析计划。
JSON 字段必须是：goal, data_sources, metrics, dimensions, filters, time_range,
grain, sort, unit, steps, output_format, result_columns, assumptions, clarification_needed,
clarification_question。
steps 中每项必须包含 id、title、kind、depends_on；kind 只能是
discover_source/load_schema/query/calculate/validate/visualize/summarize。
result_columns 只记录用户明确要求输出的列名（按原顺序），未指定时用 []。这些列是最终结果列，不是源字段；派生指标须在查询或计算步骤中产出。
不确定指标口径时不要猜，设置 clarification_needed=true，并提出简短问题。
候选表只是参考，不能编造不存在的表。

【维度约束（必须遵守）】
dimensions 和 filters 只能使用候选数据表中**真实存在的列**。
如果用户要求的维度不在列列表里（例如要求按"性别"分组，但表中没有性别列），必须：
    1) 不要把它写进 dimensions；
    2) 在 assumptions 中写明"数据源缺少【某字段】，无法支持该维度分析"；
    3) 设置 clarification_needed=true，并在 clarification_question 中询问用户如何处理。
严禁从姓名、学号等字段**推断**用户要求的维度（例如由姓名猜性别、由学号前缀猜班级）。

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
        if state.get("memory_context"):
            prompt += ("\n【相关历史，仅供参考；当前用户要求优先，历史结果不能作为当前数据】\n"
                       + state["memory_context"])
        try:
            result = llm.invoke([SystemMessage(content=prompt), HumanMessage(content=state["question"])])
            raw = _extract_json(str(result.content))
            for key in ("bound_contracts", "contract_binding", "binding", "evidence",
                        "active_issues", "issue_history", "repair_history", "quality_issues"):
                raw.pop(key, None)
            for step in raw.get("steps", []) if isinstance(raw.get("steps"), list) else []:
                if isinstance(step, dict):
                    for key in ("output", "evidence_ids", "status", "retry_count"):
                        step.pop(key, None)
            plan = AnalysisPlan.model_validate(raw)
            return {"plan": plan, "error": None}
        except Exception as exc:  # noqa: BLE001 - 规划失败必须降级，不阻断原有问答
            return {"plan": _fallback_plan(state["question"], state.get("catalog", []), str(exc)),
                    "error": str(exc)}

    def validate_node(state: AnalysisState) -> dict:
        plan = state["plan"]
        if plan is None or not plan.steps:
            return {"plan": _fallback_plan(state["question"], state.get("catalog", []), "计划没有执行步骤")}
        structural = _dependency_issues(plan, allow_missing=True)
        if structural:
            plan.error = "；".join(structural)
            plan.clarification_needed = True
            plan.quality_issues.extend(structural)
            return {"plan": plan, "error": plan.error}
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
    text = "【本轮分析计划】\n" + json.dumps(plan.model_dump(exclude_none=True), ensure_ascii=False)
    if plan.result_columns:
        text += ("\n【最终结果表】按 result_columns 产出真实工具结果。派生指标请在 SQL 中计算并使用指定别名，"
                 "不要只在回复中计算。分子、分母等可随指标一起查询用于校验。")
    return text


# 派生维度关键词：这些概念可由多列聚合得到（"科目"对应多个科目列），不做列名硬校验。
# 注意用双字词而非单字：早期用单字"年"会让"年级"被误放行（年级是真实维度），
# 漏掉的关键词会表现为"误拦"，后续按评测结果补充。
_DERIVED_DIMENSION_HINTS = (
    "科目", "学科", "课程", "科", "月份", "季度", "年度", "年份", "日期", "周次",
    "类别", "类型", "分组", "区间", "范围", "分段", "等级", "层级",
)


def _norm(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def check_dimensions_against_columns(dimensions, columns) -> list[str]:
    """校验计划要求的维度是否落在数据源真实列上。

    动机（来自对照实验）：模型会「从已有字段推断出不存在的维度」——
    按姓名猜性别、按学号前缀猜班级。这类编造能通过数据量/空结果层面的校验，
    因为模型自己构造了分组，查询确实返回了数据。本函数在**规划阶段**用确定性
    规则把它拦下来，成本远低于事后纠正。

    为避免误伤派生维度（"科目"由语文/数学等列聚合而来），命中白名单关键词直接放行。
    """
    if not columns:
        return []
    known = [_norm(col) for col in columns if _norm(col)]
    if not known:
        return []
    issues: list[str] = []
    for dim in dimensions or []:
        name = _norm(dim)
        if not name:
            continue
        if any(name in col or col in name for col in known):
            continue
        if any(hint in name for hint in _DERIVED_DIMENSION_HINTS):
            continue
        issues.append(
            f"计划要求按「{dim}」分析，但数据源列中不存在该字段"
            f"（可用列：{'、'.join(str(c) for c in columns)}）"
        )
    return issues


def _dependency_issues(plan: AnalysisPlan, allow_missing: bool = False) -> list[str]:
    ids = [step.id for step in plan.steps]
    issues = []
    if len(ids) != len(set(ids)):
        issues.append("计划步骤 ID 重复")
    graph = {step.id: step.depends_on for step in plan.steps}
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            return True
        if key in visited:
            return False
        visiting.add(key)
        for dep in graph.get(key, []):
            # Legacy unbound plans historically removed their self/missing edges.
            if allow_missing and (dep == key or dep not in graph):
                continue
            if dep not in graph:
                issues.append(f"步骤 {key} 依赖不存在的步骤 {dep}")
            elif visit(dep):
                return True
        visiting.remove(key)
        visited.add(key)
        return False

    if any(visit(key) for key in graph if key not in visited):
        issues.append("计划步骤存在依赖环")
    return list(dict.fromkeys(issues))


class PlanRuntime:
    """Controlled query evidence, contract preflight and bounded local repair.

    Only bind_contracts accepts server-selected contracts. Planner JSON is never
    treated as a trusted binding. Raw rows stay in this request's private store;
    snapshots contain provenance and stable evidence references.
    """

    QUERY_TOOLS = {"query_mysql", "query_file"}
    CALCULATE_TOOLS = {"compare_periods_tool", "compare_groups_tool", "analyze_trend_tool",
                       "detect_outliers_tool", "analyze_correlation_tool", "calculate_from_evidence"}
    VISUALIZE_TOOLS = {"chart_from_evidence", "make_chart", "generate_chart", "auto_analyze_and_visualize"}

    def __init__(self, plan: AnalysisPlan, question: str = "", on_update=None,
                 columns: Optional[List[str]] = None, *, binding=None,
                 catalog: Optional[list[dict]] = None):
        self.plan, self.question, self.on_update = plan, question, on_update
        self.columns = [str(col) for col in (columns or [])]
        self.catalog = catalog or []
        self._current: Optional[PlanStep] = None
        self._query_seen = False
        self._payloads: dict[str, dict] = {}
        self._binding = None
        self._stopped = False
        self._managed_messages: set[str] = set()
        self.last_decision = self._decision([], None)
        self.validation_status = "pending"
        self.validation_report: dict[str, Any] = {}
        # Never trust bindings or executable evidence returned in planner JSON.
        self.plan.bound_contracts = []
        for issue in check_dimensions_against_columns(plan.dimensions, self.columns):
            self._add_issue("missing_dimension", issue, action="clarify")
        for issue in _dependency_issues(plan):
            self._add_issue("invalid_dependencies", issue, action="clarify")
        if binding is not None:
            self.bind_contracts(binding)
        if plan.quality_issues:
            self._emit()

    def bind_contracts(self, binding, catalog=None):
        """Bind a trusted registry result. Never call with planner-created contracts."""
        from app.agent.contracts import ContractBinding, MetricContract
        if not hasattr(binding, "contracts"):
            binding = ContractBinding(contracts=tuple(
                item if isinstance(item, MetricContract) else MetricContract.model_validate(item)
                for item in binding
            ))
        self._binding = binding
        if catalog is not None:
            self.catalog = catalog
        self.plan.bound_contracts = [item.model_dump(mode="json") for item in binding.contracts]
        for issue in binding.issues:
            self._record_contract_issue(issue)
        self._emit()

    @property
    def has_contracts(self):
        return self._binding is not None and bool(self._binding.contracts)

    def _emit(self):
        if self.on_update:
            self.on_update(self.snapshot())

    def snapshot(self) -> dict:
        return self.plan.model_dump(exclude_none=True)

    def _sync_issues(self):
        external = [message for message in self.plan.quality_issues
                    if message not in self._managed_messages]
        messages = [issue["message"] for issue in self.plan.active_issues]
        self._managed_messages.update(messages)
        self.plan.quality_issues = list(dict.fromkeys(external + messages))

    def _add_issue(self, code, message, *, step_id=None, evidence_id=None,
                   expected=None, actual=None, action="repair"):
        key = (code, step_id, evidence_id)
        issue = next((item for item in self.plan.active_issues
                      if (item["code"], item.get("step_id"), item.get("evidence_id")) == key), None)
        if issue is None:
            issue = dict(code=code, message=message, step_id=step_id, evidence_id=evidence_id,
                         expected=expected, actual=actual, action=action, active=True,
                         version=self.plan.version)
            self.plan.active_issues.append(issue)
        self._sync_issues()
        return issue

    def _record_contract_issue(self, issue):
        item = issue.model_dump(mode="json") if hasattr(issue, "model_dump") else dict(issue)
        return self._add_issue(item["code"], item.get("message") or
                               f"{item['code']}：预期 {item.get('expected')}，实际 {item.get('actual')}",
                               step_id=item.get("step_id"), evidence_id=item.get("evidence_id"),
                               expected=item.get("expected"), actual=item.get("actual"),
                               action=item.get("action", "repair"))

    def _resolve_issues(self, step_ids, reason="repaired"):
        kept = []
        for issue in self.plan.active_issues:
            if issue.get("step_id") in step_ids:
                self.plan.issue_history.append({**issue, "active": False,
                                                "resolved_version": self.plan.version,
                                                "resolution": reason})
            else:
                kept.append(issue)
        self.plan.active_issues = kept
        self._sync_issues()

    def _step_for(self, tool: str, args=None) -> Optional[PlanStep]:
        explicit = (args or {}).get("plan_step_id") or (args or {}).get("step_id")
        if explicit:
            return next((step for step in self.plan.steps if step.id == explicit), None)
        kind = ({"list_schemas": "discover_source", "get_schema": "load_schema",
                 "get_table_schema": "load_schema",
                 "file_stats": "discover_source" if self.has_contracts else "query"}.get(tool)
                or ("query" if tool in self.QUERY_TOOLS else None)
                or ("calculate" if tool in self.CALCULATE_TOOLS else None)
                or ("visualize" if tool in self.VISUALIZE_TOOLS else None))
        eligible = [step for step in self.plan.steps if step.kind == kind
                    and step.status not in ("succeeded", "skipped")]
        ready = [step for step in eligible if all(
            next((other.status for other in self.plan.steps if other.id == dep), None) == "succeeded"
            for dep in step.depends_on)]
        return next(iter(ready or eligible), None)

    def _decision(self, issues, step):
        actions = [item.get("action", "repair") for item in issues]
        action = ("stop" if self._stopped or "stop" in actions else
                  "clarify" if "clarify" in actions else "repair" if issues else "execute")
        if action == "repair" and step is not None and step.retry_count >= 2:
            action = "stop"
        return {"allowed": action == "execute", "issues": deepcopy(issues),
                "action": action, "next_action": action,
                "should_retry": action == "repair", "step_id": step.id if step else None}

    @staticmethod
    def _evidence_ids(args):
        ids = args.get("evidence_ids")
        if ids is None and args.get("evidence_id"):
            ids = [args["evidence_id"]]
        return [ids] if isinstance(ids, str) else ids or []

    def preflight_tool(self, tool: str, args: dict, step_id=None) -> dict:
        args = dict(args or {})
        if step_id:
            args["plan_step_id"] = step_id
        step = self._step_for(tool, args)
        issues = []
        if self._stopped:
            issues.append(dict(code="execution_stopped", message="执行已终止", action="stop"))
        explicit = args.get("plan_step_id") or args.get("step_id")
        if self.has_contracts and tool in self.QUERY_TOOLS and (step is None or step.status in ("succeeded", "skipped")):
            issues.append(self._add_issue("unplanned_query_step", "查询没有待执行计划步骤，请先显式发起局部修复",
                                         step_id=step.id if step else None, action="clarify"))
        expected_kind = ("query" if tool in self.QUERY_TOOLS else
                         "calculate" if tool in self.CALCULATE_TOOLS else
                         "visualize" if tool in self.VISUALIZE_TOOLS else None)
        if step is not None and expected_kind and step.kind != expected_kind:
            issues.append(self._add_issue("step_kind_mismatch", "工具种类与计划步骤不一致",
                step_id=step.id, expected=step.kind, actual=expected_kind, action="clarify"))
        if explicit and step is None:
            issues.append(self._add_issue("unknown_step", f"不存在步骤 {explicit}", action="clarify"))
        for issue in self.plan.active_issues:
            if issue.get("action") in ("clarify", "stop"):
                issues.append(issue)
        if step:
            missing = [dep for dep in step.depends_on if next(
                (other.status for other in self.plan.steps if other.id == dep), None) != "succeeded"]
            if missing:
                issues.append(self._add_issue("dependency_not_ready", "上游步骤尚未成功",
                    step_id=step.id, expected=step.depends_on, actual=missing))
        if tool == "query_file" and self.has_contracts:
            files = [item.get("file_name") for item in self.catalog
                     if item.get("name") == "df" and item.get("file_name")]
            if len(files) > 1:
                issues.append(self._add_issue("ambiguous_file_source", "多个上传文件的 df 映射存在歧义",
                                             step_id=step.id if step else None, action="clarify"))
            elif files and args.get("file") != files[0]:
                issues.append(self._add_issue("file_source_mismatch", "查询文件与可信数据源绑定不一致",
                    step_id=step.id if step else None, expected=files[0], actual=args.get("file")))
        if tool in self.QUERY_TOOLS and self._binding is not None:
            from app.agent.contracts import validate_sql_contracts
            for issue in validate_sql_contracts(args.get("sql", ""), self._binding, self.catalog,
                    step_id=step.id if step else None,
                    dialect="mysql" if tool == "query_mysql" else "duckdb"):
                issues.append(self._record_contract_issue(issue))
        if self.has_contracts and tool in self.CALCULATE_TOOLS | self.VISUALIZE_TOOLS:
            ids = self._evidence_ids(args) or (self._evidence_ids(step.input) if step else [])
            if not ids:
                issues.append(self._add_issue("missing_evidence_reference", "计算和图表必须引用真实查询证据 ID",
                                             step_id=step.id if step else None))
            else:
                try:
                    self.evidence_rows(ids)
                except ValueError as exc:
                    issues.append(self._add_issue("invalid_evidence_reference", str(exc),
                                                 step_id=step.id if step else None))
        self.last_decision = self._decision(issues, step)
        if issues and step is not None:
            step.input = deepcopy(args)
            step.status = "failed"
            step.error = "；".join(item.get("message", item["code"]) for item in issues)
        return deepcopy(self.last_decision)

    def before_tool(self, tool: str, args: dict):
        decision = self.preflight_tool(tool, args)
        self._current = self._step_for(tool, args)
        if not decision["allowed"]:
            return decision
        if self._current:
            if self._current.status == "failed":
                if not self.request_repair(self._current.id, args, "工具重试"):
                    return self.last_decision
            self._current.status = "running"
            self._current.input = deepcopy(args or {})
            if self.has_contracts:
                # A replacement has passed preflight. Archive the previous
                # attempt's step-local errors before invoking a reference tool,
                # whose evidence gate must not reject its own corrected input.
                # Global clarify/stop issues were already rejected by preflight.
                self._resolve_issues({self._current.id}, reason="validated_replacement_started")
            repair = next((item for item in reversed(self.plan.repair_history)
                           if item["step_id"] == self._current.id and item["version"] == self.plan.version), None)
            if repair is not None:
                repair["after"] = deepcopy(self._current.input)
                repair["changed_fields"] = {
                    key: {"before": repair["before"].get(key), "after": repair["after"].get(key)}
                    for key in repair["before"].keys() | repair["after"].keys()
                    if repair["before"].get(key) != repair["after"].get(key)
                }
            self._emit()
        return decision

    @staticmethod
    def _table_payload(result: str) -> Optional[dict]:
        match = re.search(r"<!--TABLE:(.*?)-->", result or "", re.S)
        if not match:
            return None
        try:
            payload = json.loads(match.group(1))
            if not isinstance(payload, dict) or not isinstance(payload.get("columns"), list) or not isinstance(payload.get("rows"), list):
                return None
            if not all(isinstance(row, list) and len(row) == len(payload["columns"]) for row in payload["rows"]):
                return None
            return payload
        except (TypeError, ValueError):
            return None

    def get_evidence(self, evidence_id: str) -> dict:
        meta = next((item for item in self.plan.evidence if item.get("id") == evidence_id), None)
        if meta is None or not meta.get("active", True) or evidence_id not in self._payloads:
            raise ValueError(f"证据 {evidence_id} 不存在或已失效")
        return {**deepcopy(meta), "payload": deepcopy(self._payloads[evidence_id])}

    def evidence_rows(self, evidence_ids) -> dict:
        if isinstance(evidence_ids, str):
            evidence_ids = [evidence_ids]
        if not evidence_ids:
            raise ValueError("缺少证据 ID")
        if not isinstance(evidence_ids, (list, tuple)) or not all(isinstance(key, str) for key in evidence_ids):
            raise ValueError("证据 ID 必须为字符串列表")
        records = [self.get_evidence(key) for key in evidence_ids]
        if any(item.get("truncated", False) for item in records):
            raise ValueError("证据已截断，不能用于完整统计或图表")
        columns = records[0]["payload"]["columns"]
        if any(item["payload"]["columns"] != columns for item in records):
            raise ValueError("多个证据列不一致，需显式合并步骤")
        return {"columns": columns, "rows": [row for item in records for row in item["payload"]["rows"]],
                "evidence_ids": list(evidence_ids)}

    def invalidate_step(self, step_id: str, reason="upstream_repair") -> list[str]:
        if not any(step.id == step_id for step in self.plan.steps):
            raise ValueError(f"不存在步骤 {step_id}")
        affected = {step_id}
        while True:
            affected_evidence = {item.get("id") for item in self.plan.evidence
                                 if item.get("step_id") in affected}
            more = {step.id for step in self.plan.steps
                    if set(step.depends_on) & affected or set(step.evidence_ids) & affected_evidence}
            if more <= affected:
                break
            affected |= more
        for step in self.plan.steps:
            if step.id in affected:
                step.status, step.output, step.error, step.evidence_ids = "pending", None, None, []
        for item in self.plan.evidence:
            if item.get("step_id") in affected and item.get("active", True):
                item.update(active=False, invalidated_reason=reason)
        self.validation_status = "pending"
        self.validation_report = {}
        self._emit()
        return sorted(affected)

    def request_repair(self, step_id, changes=None, reason="validation_failure") -> bool:
        step = next((item for item in self.plan.steps if item.id == step_id), None)
        if step is None:
            raise ValueError(f"不存在步骤 {step_id}")
        blocking = [item for item in self.plan.active_issues
                    if item.get("step_id") in (None, step_id) and item.get("action") in ("clarify", "stop")]
        if self._stopped or blocking or step.retry_count >= 2:
            self.last_decision = self._decision(blocking or [dict(action="stop", code="repair_limit")], step)
            return False
        before = deepcopy(step.input)
        affected = self.invalidate_step(step_id)
        self.plan.version += 1
        step.retry_count += 1
        step.input.update(deepcopy(changes or {}))
        self.plan.repair_history.append({"step_id": step_id, "attempt": step.retry_count,
            "reason": reason, "before": before, "after": deepcopy(step.input),
            "invalidated_steps": affected, "version": self.plan.version})
        # Issues remain active until a replacement succeeds.
        self._emit()
        return True

    @property
    def needs_repair(self) -> bool:
        return self.should_retry()

    def should_retry(self) -> bool:
        if self._stopped or any(item.get("action") in ("clarify", "stop") for item in self.plan.active_issues):
            return False
        return any(item.get("action") == "repair" and
                   next((step.retry_count for step in self.plan.steps if step.id == item.get("step_id")), 0) < 2
                   for item in self.plan.active_issues)

    def next_action(self) -> str:
        if self._stopped:
            return "stop"
        if any(item.get("action") == "clarify" for item in self.plan.active_issues):
            return "clarify"
        return "repair" if self.should_retry() else "stop" if self.plan.active_issues else "execute"

    def repair_context(self) -> str:
        return "【局部修复】\n" + json.dumps({"action": self.next_action(),
            "issues": self.plan.active_issues, "attempt_limit": 2,
            "repairs": self.plan.repair_history}, ensure_ascii=False, default=str)

    def stop(self, reason="execution_stopped"):
        self._stopped = True
        self._add_issue("execution_stopped", reason, action="stop")
        self.validation_status = "rejected"
        self.last_decision = self._decision(self.plan.active_issues, self._current)
        self._emit()

    def after_tool(self, tool: str, args: dict, result: str) -> list[str]:
        if self._stopped:
            return ["执行已终止，结果不再接受"]
        step = self._current or self._step_for(tool, args)
        step_id = step.id if step else None
        issues: list[str] = []
        failed = ("错误" in (result or "") or "异常" in (result or "")
                  or (result or "").startswith("[TOOL_TIMEOUT]"))
        try:
            structured_result = json.loads(result)
        except (TypeError, ValueError):
            structured_result = None
        if isinstance(structured_result, dict) and (
                structured_result.get("status") == "error" or structured_result.get("success") is False):
            failed = True
        payload = self._table_payload(result)
        evidence_id = None
        if tool in self.QUERY_TOOLS:
            self._query_seen = True
            rows = payload.get("rows", []) if payload else []
            columns = payload.get("columns", []) if payload else []
            sql = (args or {}).get("sql", "")
            if not payload and not failed:
                issues.append("查询缺少真实结构化结果")
            if (payload and not rows) or (not payload and "空结果" in (result or "")):
                issues.append("查询返回空结果")
            if len(rows) < 2 and any(word in self.question for word in ("对比", "趋势", "分布", "相关")):
                issues.append("样本量不足，无法支持比较类结论")
            if payload and self.has_contracts:
                actual_sql = payload.get("executed_sql")
                submitted_sql = sql.strip().rstrip(";")
                allowed_sql = {submitted_sql}
                if not re.search(r"\blimit\b", submitted_sql, re.I):
                    allowed_sql.add(submitted_sql + " LIMIT 500")
                if not isinstance(actual_sql, str) or actual_sql.strip().rstrip(";") not in allowed_sql:
                    issues.append("真实执行 SQL 与提交查询不一致或缺少执行元数据")
                    failed = True
                expected_source = "mysql" if tool == "query_mysql" else "df"
                if payload.get("source") != expected_source:
                    issues.append("真实执行数据源与工具不一致")
                    failed = True
                if payload.get("truncated") or payload.get("row_count_total", len(rows)) > len(rows):
                    issues.append("查询证据已截断，必须缩小查询或在数据库聚合后重试")
                from app.agent.contracts import validate_sql_contracts
                for issue in validate_sql_contracts(actual_sql or sql, self._binding, self.catalog,
                        step_id=step_id, dialect="mysql" if tool == "query_mysql" else "duckdb"):
                    item = self._record_contract_issue(issue)
                    issues.append(item["message"])
                    failed = True
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
            if not self.has_contracts:
                if self.plan.filters and "where" not in sql.lower():
                    issues.append("查询未体现计划中的过滤条件")
                if self.plan.time_range and "where" not in sql.lower():
                    issues.append("查询未体现计划中的时间范围")
                if self.plan.grain and self.plan.dimensions and "group by" not in sql.lower():
                    issues.append("查询未体现计划要求的聚合粒度")
                if self.plan.sort and "order by" not in sql.lower():
                    issues.append("查询未体现计划要求的排序")
            fingerprint = hashlib.sha256(json.dumps({"source": tool, "sql": sql,
                "source_name": args.get("file") or args.get("database") or args.get("file_id"),
                "payload": payload, "step_id": step_id, "version": self.plan.version},
                ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
            evidence_id = "ev_" + fingerprint[:24]
            meta = {"id": evidence_id, "source": tool, "source_name": args.get("file") or args.get("database") or args.get("file_id"),
                    "sql": sql, "columns": columns, "row_count": len(rows), "step_id": step_id,
                    "version": self.plan.version, "fingerprint": fingerprint, "active": not failed and payload is not None,
                    "quality_issues": list(issues),
                    "executed_sql": payload.get("executed_sql") if payload else None,
                    "truncated": bool(payload and (payload.get("truncated") or payload.get("row_count_total", len(rows)) > len(rows))),
                    "row_count_total": payload.get("row_count_total", len(rows)) if payload else 0,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self.plan.evidence = [item for item in self.plan.evidence if item.get("id") != evidence_id] + [meta]
            if payload is not None and not failed:
                self._payloads[evidence_id] = deepcopy(payload)
            self.validation_status = "pending"
        if "分母为零" in (result or "") or re.search(r"/\s*0(?:\.0+)?\b", result or ""):
            issues.append("检测到分母为零风险")
        if not failed and step_id:
            self._resolve_issues({step_id})
        if failed:
            message = (result or "")[:300]
            action = "clarify" if any(word in message for word in ("字段不存在", "Unknown column", "口径不明")) else "repair"
            self._add_issue("tool_error", message, step_id=step_id, evidence_id=evidence_id, action=action)
        for index, message in enumerate(issues):
            self._add_issue(f"result_quality_{index}", message, step_id=step_id, evidence_id=evidence_id)
        if step:
            step.status = "failed" if failed or (self.has_contracts and issues) else "succeeded"
            step.evidence_ids = [evidence_id] if evidence_id else list(self._evidence_ids(args))
            step.output = {"quality_issues": issues, "evidence_ids": step.evidence_ids}
            step.error = (result or "")[:300] if failed else None
        self.last_decision = self._decision(self.plan.active_issues, step)
        self._current = None
        self._emit()
        return issues

    def result_delivery(self) -> tuple[Optional[dict], Optional[str]]:
        """Select a single verified result; independent query pages cannot prove coverage."""
        requested = self.plan.result_columns
        # Read explicit output counts from the user input, not the planner or summary.
        count = re.search(r"(?:全部|所有|完整|前|返回|列出)\s*([0-9]{1,9})\s*(?:行|条记录)", self.question)
        expected = int(count.group(1)) if count else 0
        if not requested and not expected and not re.search(r"(?:全部|所有|完整)\s*(?:行|记录|数据|明细)", self.question):
            return None, None
        candidates = []
        largest = 0
        for evidence in reversed(self.plan.evidence):
            if not evidence.get("active"):
                continue
            payload = self._payloads.get(evidence.get("id"))
            if not payload:
                continue
            columns, rows = payload.get("columns", []), payload.get("rows", [])
            if (not rows or not all(columns.count(column) == 1 for column in requested)
                    or any(len(row) != len(columns) for row in rows)):
                continue
            largest = max(largest, len(rows))
            total = payload.get("row_count_total", len(rows))
            if evidence.get("truncated") and type(total) is int:
                expected = max(expected, total)
            if not evidence.get("truncated") and total == len(rows):
                projection = requested or columns
                indices = [columns.index(column) for column in requested] if requested else range(len(columns))
                table = {**payload, "columns": projection, "rows": [[row[i] for i in indices] for row in rows]}
                candidates.append((columns == projection, table))
        if self.validation_status == "approved":
            eligible = [(exact, table) for exact, table in candidates if not expected or len(table["rows"]) == expected]
            if eligible:
                return max(eligible, key=lambda candidate: candidate[0])[1], None
        if expected and not any(len(table["rows"]) == expected for _, table in candidates):
            return None, (
                f"本轮未能验证完整交付：需要 {expected} 行，单次查询最多取得 {largest} 行，"
                "本轮展示 0 行。"
            )
        return None, None

    def validate_evidence(self, final: bool = False) -> Dict[str, Any]:
        """Validate query evidence; final also requires planned computations/plots.

        Completion issues remain active for local repair. The earlier statistical
        gate ignores only downstream completion reminders, so a missing chart
        cannot prevent the chart tool from reading otherwise valid evidence.
        """
        if final and self.has_contracts:
            for step in self.plan.steps:
                if step.kind not in ("query", "calculate", "visualize"):
                    continue
                approved_skip = (step.status == "skipped" and bool(step.output)
                                 and step.output.get("skip_approved") is True
                                 and bool(step.output.get("skip_reason")))
                if step.status != "succeeded" and not approved_skip:
                    self._add_issue("missing_required_step", f"计划步骤「{step.title}」尚未完成",
                        step_id=step.id, expected="succeeded or owner-approved skip",
                        actual=step.status, action="repair")
        self._sync_issues()
        active = [item for item in self.plan.evidence if item.get("active", True)
                  and (not self.has_contracts or item.get("id") in self._payloads)]
        deferred_messages = {
            item["message"] for item in self.plan.active_issues
            if not final and item["code"] == "missing_required_step"
            and next((step.kind for step in self.plan.steps if step.id == item.get("step_id")), None)
            in ("calculate", "visualize")
        }
        reasons = list(dict.fromkeys(message for message in self.plan.quality_issues
                                     if message not in deferred_messages))
        if not active:
            reasons.insert(0, "没有真实查询证据")
        if any(step.kind == "query" and step.status == "failed" for step in self.plan.steps):
            reasons.append("数据执行步骤失败")
        if self.has_contracts and any(step.kind == "query" and step.status in ("pending", "running")
                                      for step in self.plan.steps):
            reasons.append("计划查询步骤尚未完成")
        reasons = list(dict.fromkeys(reasons))
        approved = not reasons and not self._stopped
        self.validation_status = "approved" if approved else "rejected"
        self.validation_report = {"status": self.validation_status, "final": final,
            "evidence_count": len(active),
            "evidence": deepcopy(active), "issues": reasons,
            "structured_issues": deepcopy(self.plan.active_issues),
            "next_action": self.next_action(), "should_retry": self.should_retry()}
        for step in self.plan.steps:
            if step.kind == "validate":
                if self.has_contracts and any(next((other.status for other in self.plan.steps if other.id == dep), None)
                                               != "succeeded" for dep in step.depends_on):
                    step.status = "pending"
                    continue
                step.status = "succeeded" if approved else "failed"
                step.output = {"quality_issues": reasons,
                               "evidence_ids": [item.get("id") for item in active if item.get("id")]}
                step.error = "；".join(reasons) if reasons else None
        self._emit()
        return deepcopy(self.validation_report)

    def record_worker_failure(self, worker: str, status: str, detail: str = "") -> None:
        label = {"timeout": "执行超时", "error": "执行异常", "busy": "资源繁忙"}.get(status, status)
        issue = f"{worker}{label}" + (f"：{detail}" if detail else "")
        # Worker failures affect the whole request. A late background worker must
        # never clear a timeout/busy error by publishing apparently valid rows.
        self.stop(issue)

    def finalize(self):
        for step in self.plan.steps:
            if step.status == "pending" and step.kind in ("validate", "summarize"):
                step.status = "succeeded" if self.validation_status == "approved" and not self._stopped else "failed"
                step.output = {"quality_issues": list(self.plan.quality_issues)}
        self._emit()
