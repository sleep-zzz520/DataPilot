from __future__ import annotations
import asyncio, re, json, io, uuid, threading, time
from collections import OrderedDict
from pathlib import Path
from typing import Optional, List, Literal
from hashlib import sha256
import pandas as pd
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langchain_core.messages import (
    HumanMessage, AIMessage, AIMessageChunk, ToolMessage, SystemMessage,
)
from app.meta.store import get_llm_secret, get_db_secret
from app.core.factories import build_llm, build_engine
from app.tools.agent_tools import make_tools
from app.agent.multi_agent import make_agent
from app.errors.classifier import classify_any
from app.audit import record, A_FILE_UPLOAD, A_SESSION_ACTION, A_HITL_CONFIRMATION
from app.usage import check_budget, record_usage, extract_usage
from app.usage.callback import RequestUsage, with_request_usage
from app.memory.context import assemble_context
from app.memory.agent_memory import (
    build_memory_context, extract_and_store_memories, summary_due, generate_summary,
    save_analysis_record, delete_analysis_records,
)
from app.db.schema import list_tables
from app.agent.analysis_plan import AnalysisPlan, make_analysis_plan_graph, PlanRuntime, plan_context
from app.agent.contracts import MetricContractRegistry, ContractBinding, ValidationIssue, bind_metric_contracts
from app.agent.contract_catalog import load_contract_catalog
from app.agent.hitl import assess_confirmation, request_refusal
from app.memory import get_history, save_messages, clear_session
from app.memory.store import budget_history, estimate_tokens, MAX_TOKENS
from app.agent.graph import _filter_new_messages
from app.agent.trace import TraceCollector
from app.agent.prompts import SUPERVISOR_PROMPT, CONVERSATION_HINT
# ── 对话记录持久化（SQLite 镜像，不影响现有记忆系统）─────────────────────────
from app.persistence import (
    persist_messages,
    persist_guarded_response,
    load_session as db_load_session,
    list_sessions as db_list_sessions,
    delete_session as db_delete_session,
    rename_session as db_rename_session,
    get_display_messages,
    get_session_info,
    save_upload,
    get_upload,
    save_trace,
    load_traces,
    save_pending_approval,
    consume_pending_approval,
)
from app.api.auth_api import get_current_user

router = APIRouter()

# 上传文件落盘目录 + DataFrame 懒加载缓存（避免每次请求重复解析）
_BACKEND_DIR = Path(__file__).resolve().parents[2]  # backend/
_UPLOAD_DIR = _BACKEND_DIR / "data" / "uploads"
_CONTRACT_CONFIG = _BACKEND_DIR / "config" / "metric_contracts.json"
_file_df_cache: "OrderedDict[str, pd.DataFrame]" = OrderedDict()
_file_df_cache_lock = threading.Lock()
_FILE_DF_CACHE_MAX = 12


def _load_uploaded_dfs(file_ids: List, user_id: int) -> dict:
    """按 file_id 加载当前用户的上传文件 DataFrame（磁盘 + LRU 缓存）。"""
    dfs: dict = {}
    for fid in file_ids:
        with _file_df_cache_lock:
            df = _file_df_cache.get(fid)
        if df is not None:
            meta = get_upload(str(fid), user_id)
            if meta:
                dfs[meta["name"]] = df
            continue
        meta = get_upload(str(fid), user_id)
        if meta is None:
            continue
        try:
            ext = Path(meta["path"]).suffix.lower()
            if ext == ".csv":
                df = pd.read_csv(meta["path"])
            else:
                df = pd.read_excel(meta["path"])
        except Exception:
            continue
        with _file_df_cache_lock:
            _file_df_cache[fid] = df
            if len(_file_df_cache) > _FILE_DF_CACHE_MAX:
                _file_df_cache.popitem(last=False)
        dfs[meta["name"]] = df
    return dfs


def _uploaded_file_paths(file_ids: List, user_id: int) -> dict:
    """返回当前用户上传文件的受控落盘路径，供本地工具子进程重读。"""
    paths: dict = {}
    for fid in file_ids:
        meta = get_upload(str(fid), user_id)
        if meta and meta.get("name") and meta.get("path"):
            paths[meta["name"]] = meta["path"]
    return paths

# 每个 session 一个 asyncio 锁：串行化同一会话的对话处理，
# 避免并发请求交错读写内存/SQLite 导致消息错乱（串话、重复）。
_chat_locks: dict = {}
_chat_locks_guard: Optional[asyncio.Lock] = None

# ── 同请求防重（30s 窗口）：同会话 + 相同消息 + 相同 file_ids 视为重复提交，
# 直接拒绝不调 LLM（防多 tab/重放导致的历史重复分析与 token 浪费）────────────
_DEDUP_WINDOW_SECONDS = 30
_dedup: dict = {}
_dedup_lock = threading.Lock()


def _check_duplicate(sid: str, message: str, file_ids: Optional[list],
                     approval_id: Optional[str] = None,
                     approval_decision: Optional[str] = None) -> Optional[str]:
    """记录本次请求键；30 秒内同会话相同请求返回提示，否则返回 None（放行）。"""
    base = (sid, (message or "").strip(), tuple(sorted(file_ids or [])))
    key = base + (approval_id, approval_decision) if approval_id else base
    now = time.time()
    with _dedup_lock:
        # 顺手清理过期键，防止字典无限增长
        for k in list(_dedup):
            if now - _dedup[k] > _DEDUP_WINDOW_SECONDS:
                del _dedup[k]
        if key in _dedup:
            return "检测到重复提交，请等待当前回复完成后稍后再试"
        _dedup[key] = now
    return None


async def _session_lock(sid: str) -> asyncio.Lock:
    global _chat_locks_guard
    # 惰性创建：asyncio.Lock 在 Python 3.9 构造时绑定当前事件循环，
    # 模块级直接创建会导致无事件循环的环境（pytest/脚本）导入即崩。
    if _chat_locks_guard is None:
        _chat_locks_guard = asyncio.Lock()
    async with _chat_locks_guard:
        lock = _chat_locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            _chat_locks[sid] = lock
        return lock

def _release_session_lock(sid: str) -> None:
    """会话被删除时释放其锁（避免字典无限增长）。"""
    _chat_locks.pop(sid, None)

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    llm_config_id: int
    db_config_id: int
    file_ids: List = []
    approval_id: Optional[str] = None
    approval_decision: Optional[Literal["approve", "reject"]] = None
    agent_mode: Literal["auto", "single", "multi"] = "auto"
    metric_contract_ids: List[str] = []

_CHART_RE = re.compile(r"<!--CHART:(.*?)-->", re.S)
_TABLE_RE = re.compile(r"<!--TABLE:(.*?)-->", re.S)
_IMAGE_RE = re.compile(r"<!--IMAGE_BASE64:(.*?)-->", re.S)

def _build_plan(llm, engine, question: str, schema: Optional[str], file_ids: list, user_id: int,
                memory_context: str = ""):
    """规划失败只降级，不阻断原有 Agent；返回计划和 SSE 事件。"""
    try:
        catalog = list_tables(engine, schema)
        for fid in file_ids:
            meta = get_upload(str(fid), user_id)
            if meta:
                catalog.append({"name": meta["name"], "comment": "上传文件", "columns": meta.get("columns", [])})
    except Exception:
        catalog = []
    result = make_analysis_plan_graph(llm).invoke({
        "question": question, "catalog": catalog, "plan": None, "error": None,
        "memory_context": memory_context,
    })
    plan = result["plan"]
    return plan, {"type": "plan", "event": "plan_created", "plan": plan.model_dump(exclude_none=True)}


def _request_hash(req: ChatRequest, sid: str) -> str:
    """确认必须绑定原始请求和配置，避免 token 被换会话/换数据源重放。"""
    raw = json.dumps({
        "session_id": sid,
        "message": (req.message or "").strip(),
        "file_ids": sorted(str(fid) for fid in (req.file_ids or [])),
        "llm_config_id": req.llm_config_id,
        "db_config_id": req.db_config_id,
        "agent_mode": req.agent_mode,
        "metric_contract_ids": sorted(req.metric_contract_ids),
        "contract_registry_sha256": sha256(_CONTRACT_CONFIG.read_bytes()).hexdigest() if _CONTRACT_CONFIG.exists() else None,
    }, ensure_ascii=False, sort_keys=True)
    return sha256(raw.encode("utf-8")).hexdigest()


def _memory_scope(req: ChatRequest) -> str:
    """Preferences and reusable analyses must not bleed across data configurations."""
    return "db:{}:files:{}".format(req.db_config_id, ",".join(sorted(str(f) for f in req.file_ids)))


def _load_contract_registry():
    config = json.loads(_CONTRACT_CONFIG.read_text()) if _CONTRACT_CONFIG.exists() else {}
    return MetricContractRegistry.from_config(config), config.get("source_bindings", {})


def _bind_request_contracts(req, plan, engine, user):
    registry, policies = _load_contract_registry()
    if not req.metric_contract_ids and not registry.enabled():
        return None, []  # Legacy requests remain usable until owner configures definitions.
    selected = [registry.get(key) for key in req.metric_contract_ids]
    selected = [item for item in selected if item is not None] or list(registry.enabled())
    catalog, issues = [], []
    uploads = [get_upload(str(fid), user["uid"]) for fid in req.file_ids]
    uploads = [item for item in uploads if item]
    if len(uploads) == 1:
        catalog.append({"name": "df", "columns": uploads[0].get("columns", []), "file_name": uploads[0]["name"]})
    sources = {c.source for c in selected if c.source != "df"}
    sources.update(join.source for c in selected for join in c.join_constraints)
    if sources:
        catalog.extend(load_contract_catalog(engine, sources))
    binding = bind_metric_contracts(req.message, plan, catalog, registry,
                                    contract_ids=req.metric_contract_ids or None)
    for contract in binding.contracts:
        policy = policies.get(contract.id, {})
        valid = (len(uploads) == 1 and uploads[0]["name"] in policy.get("file_names", [])) if contract.source == "df" else req.db_config_id in policy.get("db_config_ids", [])
        if not valid:
            issues.append(ValidationIssue(code="CONTRACT_SOURCE_BINDING_REQUIRED", expected=contract.id,
                                          actual="current source is not configured", action="clarify"))
    if issues:
        binding = binding.model_copy(update={"issues": binding.issues + tuple(issues)})
    plan.bound_contracts = [contract.model_dump(mode="json") for contract in binding.contracts]
    if binding.issues:
        plan.clarification_needed = True
        plan.clarification_question = "指标契约尚不能满足：请确认已启用的指标定义、必要字段及授权数据源。"
    return binding, catalog


def _audit_hitl(user: dict, sid: str, status: str, confirmation: dict,
                approval_id: Optional[str] = None) -> None:
    record(A_HITL_CONFIRMATION, user["uid"], user.get("username"), {
        "status": status,
        "approval_id": approval_id,
        "session_id": sid,
        "reasons": confirmation.get("reasons", []),
        "data_scope": confirmation.get("data_scope", []),
        "estimated_cost": confirmation.get("estimated_cost", {}),
    })


def _request_refusal_response(req: ChatRequest, sid: str, user: dict) -> Optional[dict]:
    refusal = request_refusal(req.message)
    if refusal is None:
        return None
    reason, reply = refusal
    confirmation = {"status": "rejected", "reasons": [reason]}
    _audit_hitl(user, sid, "policy_refused", confirmation)
    return {"reply": reply, "visuals": [], "tables": [], "chart": None, "table": None,
            "sql": None, "session_id": sid, "confirmation": confirmation,
            "plan": None, "evidence": [], "trace": []}


def _resolve_plan(req: ChatRequest, sid: str, llm, engine, schema: Optional[str], user: dict) -> dict:
    """计划生成后的执行门禁；返回 ready/confirmation/rejected，拒绝不创建工具调用。"""
    request_hash = _request_hash(req, sid)
    if req.approval_id:
        if not req.approval_decision:
            raise ValueError("确认请求缺少 approval_decision")
        result = consume_pending_approval(
            req.approval_id, user["uid"], sid, request_hash, req.approval_decision,
        )
        if result["status"] == "invalid":
            raise ValueError(result["reason"])
        confirmation = result["payload"]["confirmation"]
        _audit_hitl(user, sid, result["status"], confirmation, req.approval_id)
        if result["status"] == "rejected":
            return {"kind": "rejected", "confirmation": confirmation}
        plan_data = result["payload"]["plan"]
        resumed = AnalysisPlan.model_validate(plan_data)
        binding, catalog = _bind_request_contracts(req, resumed, engine, user)
        if binding is not None and binding.issues:
            return {"kind": "confirmation", "plan": resumed, "confirmation": assess_confirmation(req.message, resumed),
                    "plan_event": {"type": "plan", "plan": resumed.model_dump()}, "binding": binding, "catalog": catalog}
        return {
            "kind": "ready",
            "plan": resumed,
            "binding": binding, "catalog": catalog,
            "plan_event": {"type": "plan", "event": "plan_resumed", "plan": plan_data},
        }

    memory_context = build_memory_context(user["uid"], sid, req.message, scope=_memory_scope(req)) or ""
    registry, _ = _load_contract_registry()
    if registry.enabled():
        memory_context += "\n【服务端已启用指标定义；禁止自行修改，仅选择与问题相符的指标】\n" + json.dumps(
            [c.model_dump(mode="json") for c in registry.enabled()], ensure_ascii=False)
    _ensure_loaded(sid, user["uid"])
    recent = budget_history([m for m in get_history(sid) if isinstance(m, (HumanMessage, AIMessage))
                             and not getattr(m, "tool_calls", None)], 1000)
    if recent:
        memory_context += "\n【近期对话参考；当前用户要求优先】\n" + "\n".join(
            f"[{m.type}] {m.content}" for m in recent)
    plan, plan_event = _build_plan(llm, engine, req.message, schema, req.file_ids, user["uid"], memory_context)
    binding, catalog = _bind_request_contracts(req, plan, engine, user)
    plan_event["plan"] = plan.model_dump(exclude_none=True)
    file_metadata = [get_upload(str(fid), user["uid"]) or {} for fid in req.file_ids]
    confirmation = assess_confirmation(req.message, plan, file_metadata)
    if not confirmation.get("required"):
        return {"kind": "ready", "plan": plan, "plan_event": plan_event, "binding": binding, "catalog": catalog}
    if confirmation.get("mode") == "clarification":
        _audit_hitl(user, sid, "clarification_required", confirmation)
        return {"kind": "confirmation", "plan": plan, "confirmation": confirmation,
                "plan_event": plan_event}

    approval_id = str(uuid.uuid4())
    confirmation = {**confirmation, "approval_id": approval_id, "status": "pending"}
    save_pending_approval(
        approval_id, user["uid"], sid, request_hash,
        {"plan": plan.model_dump(exclude_none=True), "confirmation": confirmation},
    )
    _audit_hitl(user, sid, "requested", confirmation, approval_id)
    return {"kind": "confirmation", "plan": plan, "confirmation": confirmation,
            "plan_event": plan_event}


# 超时类质量问题：用户侧应看到"请重试"，而不是"数据质量不通过"。
_TIMEOUT_MARKERS = ("超时", "timeout")


def _guard_reply(reply: str, plan, validation_status: Optional[str] = None, *, tool_timed_out=False) -> str:
    """质量校验未通过时，把限制明确呈现给用户，避免模型只给确定性结论。"""
    warnings = list(dict.fromkeys(plan.quality_issues))
    if validation_status and validation_status != "approved" and (
        tool_timed_out or any(mark in item for item in warnings for mark in _TIMEOUT_MARKERS)
    ):
        return (
            "本轮分析在等待查询或验证时超时，未能完成校验，因此暂不给出确定性结论。"
            "建议缩小查询范围（例如限定时间区间或减少分组）后重试。"
        )
    if plan.metrics and validation_status and validation_status != "approved":
        detail = "；".join(warnings) if warnings else "尚未完成真实证据的统计验证"
        return "本轮统计验证未通过，暂不生成确定性结论。原因：" + detail + "。"
    if warnings:
        return (reply or "") + "\n\n> 结果质量提示：" + "；".join(warnings) + "。以上结论仅供参考，请补充数据或调整口径后重试。"
    if plan.metrics and not plan.evidence:
        return (reply or "") + "\n\n> 证据提示：本轮没有获得可验证的查询结果，无法确认定量结论。"
    return reply

def _extract(messages):
    """从工具结果中提取本轮所有图表/表格/SQL（支持一轮多个，不再覆盖）。"""
    visuals: List[dict] = []   # [{chart, image}]
    tables: List[dict] = []
    sql = None
    for m in messages:
        if isinstance(m, ToolMessage) and m.content:
            item: dict = {}
            mc = _CHART_RE.search(m.content)
            if mc:
                try: item["chart"] = json.loads(mc.group(1))
                except Exception: pass
            mi = _IMAGE_RE.search(m.content)
            if mi:
                item["image"] = mi.group(1)
            if item:
                visuals.append(item)
            for mt in _TABLE_RE.finditer(m.content):
                try: tables.append(json.loads(mt.group(1)))
                except Exception: pass
        if isinstance(m, AIMessage):
            for tc in (getattr(m, "tool_calls", None) or []):
                if tc.get("name") in ("query_mysql", "run_sql"):
                    sql = tc.get("args", {}).get("sql") or sql
    return visuals, tables, sql

def _display_result(messages, reply, runtime, trace=()):
    visuals, tables, sql = _extract(messages)
    status = runtime.validation_status
    if status != "approved":
        visuals, tables = [], []
    reply = _guard_reply(reply, runtime.plan, status,
                         tool_timed_out=any(entry.get("status") == "timeout" for entry in trace))
    table, delivery_issue = runtime.result_delivery()
    if delivery_issue:
        return delivery_issue, [], [], None
    if table is not None:
        tables = [table]
        sql = table.get("executed_sql")
    elif status == "approved" and runtime.plan.result_columns:
        columns = "、".join(runtime.plan.result_columns)
        return f"本轮未取得包含所需列（{columns}）的完整查询结果，暂不提供结论。", [], [], None
    return reply, visuals, tables, sql


# ── 辅助：若内存 store 无该 session 但 SQLite 有，则从 SQLite 恢复 ──────────
def _ensure_loaded(sid: str, user_id: Optional[int]) -> None:
    """保证内存 store 中有该 session 的历史（从 SQLite 懒加载，校验归属）。"""
    if get_history(sid):
        return
    db_msgs = db_load_session(sid, user_id)
    if db_msgs:
        save_messages(sid, db_msgs)


def _ensure_session_access(sid: Optional[str], uid: int) -> None:
    """会话存在但非本人 → 403；不存在（新会话）→ 放行。"""
    if not sid:
        return
    if get_session_info(sid) and not get_session_info(sid, uid):
        raise HTTPException(403, "无权访问该会话")


def _record_request_usage(usage, req, sid, user):
    if usage is None:
        return
    cfg = get_llm_secret(req.llm_config_id) or {}
    usage.finalize(lambda input_tokens, output_tokens: record_usage(
        user["uid"], user.get("username"), sid, req.llm_config_id,
        cfg.get("provider"), cfg.get("model_name"), input_tokens, output_tokens))


@router.post("/api/chat")
async def chat(req: ChatRequest, user: dict = Depends(get_current_user)):
    db_cfg = _require_db_secret(req.db_config_id, user)
    sid = req.session_id or str(uuid.uuid4())
    _ensure_session_access(req.session_id, user["uid"])
    lock = await _session_lock(sid)
    async with lock:  # 同一会话串行处理，避免并发交错
        usage = None
        try:
            # ── 同请求防重：30s 内相同请求拒绝，不调 LLM ──────────────────
            dup = _check_duplicate(sid, req.message, req.file_ids, req.approval_id, req.approval_decision)
            if dup:
                return {"reply": None, "chart": None, "table": None, "sql": None, "session_id": sid,
                        "error": {"code": "DUPLICATE_REQUEST", "message": "重复提交",
                                  "suggestion": dup, "severity": "warn"}}
            refusal = _request_refusal_response(req, sid, user)
            if refusal is not None:
                return refusal
            # ── 预算限流：超限直接拒绝，不调 LLM ──────────────────────────
            ok, reason = check_budget()
            if not ok:
                return {"reply": None, "chart": None, "table": None, "sql": None, "session_id": sid,
                        "error": {"code": "BUDGET_LIMIT", "message": "模型调用预算已用完",
                                  "suggestion": reason, "severity": "block"}}
            usage = RequestUsage()
            llm = with_request_usage(build_llm(get_llm_secret(req.llm_config_id)), usage)
            engine = build_engine(db_cfg)
            resolution = _resolve_plan(req, sid, llm, engine, db_cfg.get("default_schema"), user)
            plan = resolution.get("plan")
            if resolution["kind"] == "rejected":
                return {"reply": "已拒绝本次分析，未执行查询。", "visuals": [], "tables": [],
                        "chart": None, "table": None, "sql": None, "session_id": sid,
                        "confirmation": {**resolution["confirmation"], "status": "rejected"},
                        "plan": plan.model_dump(exclude_none=True) if plan else None}
            if resolution["kind"] == "confirmation":
                confirmation = resolution["confirmation"]
                return {"reply": confirmation.get("prompt") or "执行前需要你的确认。",
                        "visuals": [], "tables": [], "chart": None, "table": None, "sql": None,
                        "session_id": sid, "confirmation": confirmation,
                        "plan": plan.model_dump(exclude_none=True), "evidence": [], "trace": []}

            # 上传文件 → DataFrame 字典（供 file_tool 真实查询，磁盘+LRU）
            uploaded_files = _load_uploaded_dfs(req.file_ids, user["uid"])
            uploaded_file_paths = _uploaded_file_paths(req.file_ids, user["uid"])
            tools = make_tools(engine, db_cfg.get("default_schema"), files=uploaded_files,
                               audit_ctx={"user_id": user["uid"], "username": user.get("username"), "session_id": sid},
                               file_paths=uploaded_file_paths)
            ctx = ""
            for fid in req.file_ids:
                meta = get_upload(str(fid), user["uid"])
                if meta:
                    ctx += f"\n[用户上传文件 {meta['name']}] 列：{meta['columns']} 预览行：{meta['preview_rows']}\n"
            user_text = (ctx + "\n" + req.message) if ctx else req.message
            # P0：把上传文件的真实列名交给 PlanRuntime，用于拦截"编造数据源不存在的维度"
            data_columns: List[str] = []
            for _df in uploaded_files.values():
                data_columns.extend([str(col) for col in _df.columns])
            runtime = PlanRuntime(plan, req.message, columns=data_columns,
                                  binding=resolution.get("binding"), catalog=resolution.get("catalog"))
            trace = TraceCollector()
            graph, supervisor_prompt = make_agent(llm, tools, trace=trace, plan_runtime=runtime,
                                                   mode=req.agent_mode, question=req.message)

            # ── 多轮对话：先从 SQLite 懒加载（服务重启后自动恢复）───────────────
            _ensure_loaded(sid, user["uid"])
            history = get_history(sid)
            # ── 上下文压缩：token 预算制截断（替代纯轮数截断），
            # 历史 + 当前消息总估算 token 不超过预算，防止上下文无限膨胀 ──
            system_prompt = supervisor_prompt + "\n\n" + CONVERSATION_HINT + "\n\n" + plan_context(plan)
            # ── 记忆注入：长期记忆（跨会话）+ 会话摘要 + 相关历史检索 ──────
            memory_ctx = build_memory_context(user["uid"], sid, req.message, scope=_memory_scope(req))
            input_messages = assemble_context(system_prompt, history, user_text, tools=tools,
                                              memory_context=memory_ctx,
                                              output_reserve=int(getattr(llm, "max_tokens", None) or 1024),
                                              max_tokens=16000).messages

            result = await asyncio.to_thread(graph.invoke, {"messages": input_messages})
            result_messages = result["messages"]
            runtime.finalize()

            # 提取本轮新增消息并保存到内存记忆（包括用户输入和 Agent 回复）
            new_msgs = _filter_new_messages(input_messages, result_messages)
            user_msg = HumanMessage(content=user_text, id=str(uuid.uuid4()))
            for m in new_msgs:
                if not getattr(m, "id", None):
                    m.id = str(uuid.uuid4())  # 稳定 ID，供 SQLite 幂等去重
            save_messages(sid, [user_msg] + new_msgs)

            # ── 持久化：本轮完整消息写入 SQLite（幂等去重）。
            # 不用 get_history：内存 store 可能被 token 预算裁剪（大工具输出），
            # 裁剪只影响内存输入，持久化必须保留完整历史 ──
            persist_messages(sid, [user_msg] + list(new_msgs), user["uid"], pending_guard=True)
            # ── Agent 轨迹落库：重开会话可回看工具调用链 ────────────────
            save_trace(sid, trace.snapshot(), user["uid"])

            msgs = result_messages
            reply = msgs[-1].content if isinstance(msgs[-1], AIMessage) else ""
            reply, visuals, tables, sql = _display_result(new_msgs, reply, runtime, trace.snapshot())
            persist_guarded_response(sid, user["uid"], user_msg.id, reply,
                                     visuals=visuals, tables=tables, sql=sql)

            # ── 记忆沉淀：节流提取长期事实 + 节流更新会话摘要 ────────────
            extract_and_store_memories(llm, user["uid"], user.get("username"), sid,
                                       req.message, reply, scope=_memory_scope(req))
            if summary_due(sid):
                generate_summary(llm, sid, result_messages)
            save_analysis_record(user["uid"], sid, req.message, plan, plan.evidence, reply, scope=_memory_scope(req))

            # chart/table 字段保留兼容（取最后一个），前端主用 visuals/tables 数组
            return {
                "reply": reply,
                "visuals": visuals,
                "tables": tables,
                "chart": visuals[-1].get("chart") if visuals else None,
                "table": tables[-1] if tables else None,
                "sql": sql,
                "session_id": sid,
                "plan": plan.model_dump(exclude_none=True),
                "evidence": plan.evidence,
                "trace": trace.snapshot(),
                "routing": getattr(graph, "route_decision", {}),
            }
        except Exception as e:
            return {"reply": None, "chart": None, "table": None, "sql": None, "session_id": sid, "error": classify_any(e)}
        finally:
            _record_request_usage(usage, req, sid, user)


def _sse(obj: dict) -> str:
    """SSE 事件帧。"""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@router.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    """流式对话接口（SSE）。

    事件类型（JSON 的 type 字段）：
      - delta: {"type":"delta","text":"..."}      回复文本增量
      - trace: {"type":"trace","entries":[...]}   本轮工具调用链增量（实时展示）
      - done:  {"type":"done","reply","chart","table","sql","session_id"}  最终结果
        confirmation 字段存在时表示已暂停，必须带 approval_id + approval_decision 重试
      - error: {"type":"error","error":{...},"session_id"}                 异常
    """
    db_cfg = _require_db_secret(req.db_config_id, user)
    sid = req.session_id or str(uuid.uuid4())
    _ensure_session_access(req.session_id, user["uid"])
    lock = await _session_lock(sid)

    async def gen():
        async with lock:  # 同一会话串行处理，避免并发交错
            usage = None
            try:
                # ── 同请求防重：30s 内相同请求拒绝，不调 LLM ──────────────
                dup = _check_duplicate(sid, req.message, req.file_ids, req.approval_id, req.approval_decision)
                if dup:
                    yield _sse({"type": "error",
                                "error": {"code": "DUPLICATE_REQUEST", "message": "重复提交",
                                          "suggestion": dup, "severity": "warn"},
                                "session_id": sid})
                    return
                refusal = _request_refusal_response(req, sid, user)
                if refusal is not None:
                    yield _sse({"type": "done", **refusal})
                    return
                # ── 预算限流：超限直接拒绝，不调 LLM ──────────────────────
                ok, reason = check_budget()
                if not ok:
                    yield _sse({"type": "error",
                                "error": {"code": "BUDGET_LIMIT", "message": "模型调用预算已用完",
                                          "suggestion": reason, "severity": "block"},
                                "session_id": sid})
                    return
                usage = RequestUsage()
                llm = with_request_usage(build_llm(get_llm_secret(req.llm_config_id)), usage)
                engine = build_engine(db_cfg)
                resolution = _resolve_plan(req, sid, llm, engine, db_cfg.get("default_schema"), user)
                plan = resolution.get("plan")
                if resolution["kind"] == "rejected":
                    yield _sse({"type": "done", "reply": "已拒绝本次分析，未执行查询。",
                                "visuals": [], "tables": [], "chart": None, "table": None, "sql": None,
                                "session_id": sid,
                                "confirmation": {**resolution["confirmation"], "status": "rejected"},
                                "plan": None, "evidence": [], "trace": []})
                    return
                if resolution.get("plan_event"):
                    yield _sse(resolution["plan_event"])
                if resolution["kind"] == "confirmation":
                    confirmation = resolution["confirmation"]
                    yield _sse({"type": "done", "reply": confirmation.get("prompt") or "执行前需要你的确认。",
                                "visuals": [], "tables": [], "chart": None, "table": None, "sql": None,
                                "session_id": sid, "confirmation": confirmation,
                                "plan": plan.model_dump(exclude_none=True), "evidence": [], "trace": []})
                    return

                # 上传文件 → DataFrame 字典（供 file_tool 真实查询）
                uploaded_files = _load_uploaded_dfs(req.file_ids, user["uid"])
                uploaded_file_paths = _uploaded_file_paths(req.file_ids, user["uid"])
                tools = make_tools(engine, db_cfg.get("default_schema"), files=uploaded_files,
                                   audit_ctx={"user_id": user["uid"], "username": user.get("username"), "session_id": sid},
                                   file_paths=uploaded_file_paths)
                ctx = ""
                for fid in req.file_ids:
                    meta = get_upload(str(fid), user["uid"])
                    if meta:
                        ctx += f"\n[用户上传文件 {meta['name']}] 列：{meta['columns']} 预览行：{meta['preview_rows']}\n"
                user_text = (ctx + "\n" + req.message) if ctx else req.message
                # P0：把上传文件的真实列名交给 PlanRuntime，用于拦截"编造数据源不存在的维度"
                data_columns: List[str] = []
                for _df in uploaded_files.values():
                    data_columns.extend([str(col) for col in _df.columns])
                plan_updates = []
                runtime = PlanRuntime(plan, req.message, columns=data_columns,
                                      binding=resolution.get("binding"), catalog=resolution.get("catalog"),
                                      on_update=lambda snapshot: plan_updates.append(snapshot))
                trace = TraceCollector()
                graph, supervisor_prompt = make_agent(llm, tools, trace=trace, plan_runtime=runtime,
                                                       mode=req.agent_mode, question=req.message)

                _ensure_loaded(sid, user["uid"])
                history = get_history(sid)
                # ── 上下文压缩：token 预算制截断（与同步接口一致）─────────
                system_prompt = supervisor_prompt + "\n\n" + CONVERSATION_HINT + "\n\n" + plan_context(plan)
                # ── 记忆注入：长期记忆 + 会话摘要 + 相关历史检索（与同步一致）─
                memory_ctx = build_memory_context(user["uid"], sid, req.message, scope=_memory_scope(req))
                input_messages = assemble_context(system_prompt, history, user_text, tools=tools,
                                                  memory_context=memory_ctx,
                                                  output_reserve=int(getattr(llm, "max_tokens", None) or 1024),
                                                  max_tokens=16000).messages

                # 双流模式：
                #  - "messages"：token 级文本 → 推送给前端（打字机效果）
                #  - "updates" ：每步完整消息 → 本地收集，用于保存/提取图表
                all_new: List = []
                trace_sent: int = 0
                plan_sent: int = 0
                async for mode, data in graph.astream(
                    {"messages": input_messages},
                    stream_mode=["messages", "updates"],
                ):
                    if mode == "messages":
                        chunk, _meta = data
                        # Text is emitted only after the final validation gate. Worker and
                        # rejected candidate text must never reach the user as SSE deltas.
                        pass
                    elif mode == "updates":
                        for _node, val in data.items():
                            msgs = val.get("messages") or []
                            if msgs:
                                all_new.extend(msgs)
                        # 该节点已完成：把新增的工具调用链增量推给前端（实时时间线）
                        entries = trace.snapshot()
                        if len(entries) > trace_sent:
                            yield _sse({"type": "trace", "entries": entries[trace_sent:]})
                            trace_sent = len(entries)
                        if len(plan_updates) > plan_sent:
                            for snapshot in plan_updates[plan_sent:]:
                                yield _sse({"type": "plan", "event": "step_updated", "plan": snapshot})
                            plan_sent = len(plan_updates)

                # ── 保存（内存 + SQLite），与同步接口一致 ────────────────
                user_msg = HumanMessage(content=user_text, id=str(uuid.uuid4()))
                for m in all_new:
                    if not getattr(m, "id", None):
                        m.id = str(uuid.uuid4())
                save_messages(sid, [user_msg] + list(all_new))
                # ── 持久化：本轮完整消息写入 SQLite（幂等去重），
                # 不依赖内存 store（可能被 token 预算裁剪）──
                persist_messages(sid, [user_msg] + list(all_new), user["uid"], pending_guard=True)
                # ── Agent 轨迹落库：重开会话可回看工具调用链 ────────────
                save_trace(sid, trace.snapshot(), user["uid"])

                # ── 提取图表/SQL，发送最终结果 ────────────────────────────
                result_messages = list(input_messages) + list(all_new)
                runtime.finalize()
                reply = ""
                for m in reversed(result_messages):
                    if isinstance(m, AIMessage) and m.content:
                        reply = m.content
                        break
                reply, visuals, tables, sql = _display_result(all_new, reply, runtime, trace.snapshot())
                persist_guarded_response(sid, user["uid"], user_msg.id, reply,
                                         visuals=visuals, tables=tables, sql=sql)
                if reply:
                    yield _sse({"type": "delta", "text": reply})

                # ── 记忆沉淀：节流提取长期事实 + 节流更新会话摘要 ──────────
                extract_and_store_memories(llm, user["uid"], user.get("username"), sid,
                                           req.message, reply, scope=_memory_scope(req))
                if summary_due(sid):
                    generate_summary(llm, sid, result_messages)
                save_analysis_record(user["uid"], sid, req.message, plan, plan.evidence, reply, scope=_memory_scope(req))

                yield _sse({
                    "type": "done",
                    "reply": reply,
                    "visuals": visuals,
                    "tables": tables,
                    "chart": visuals[-1].get("chart") if visuals else None,
                    "table": tables[-1] if tables else None,
                    "sql": sql,
                    "session_id": sid,
                    "plan": plan.model_dump(exclude_none=True),
                    "evidence": plan.evidence,
                    "trace": trace.snapshot(),
                    "routing": getattr(graph, "route_decision", {}),
                })
            except Exception as e:
                yield _sse({"type": "error", "error": classify_any(e), "session_id": sid})
            finally:
                _record_request_usage(usage, req, sid, user)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/upload")
async def upload(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    name = file.filename or "file"
    raw = await file.read()
    ext = name.rsplit(".", 1)[-1].lower()
    try:
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(raw))
        elif ext in ("xlsx", "xls"):
            df = pd.read_excel(io.BytesIO(raw))
        else:
            raise ValueError(f"暂不支持 .{ext}，请上传 csv/xlsx")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{e}")
    fid = str(uuid.uuid4())
    # 文件本体落盘（重启不丢），元信息写入 SQLite
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_path = _UPLOAD_DIR / f"{fid}.{ext}"
    file_path.write_bytes(raw)
    cols = list(df.columns)
    d2 = df.head(5)
    preview = d2.where(d2.notna(), None).values.tolist()
    save_upload(fid, name, str(file_path), cols, preview, user["uid"])
    record(A_FILE_UPLOAD, user["uid"], user.get("username"),
           {"file_id": fid, "name": name, "columns": cols, "rows": len(df)})
    return {"file_id": fid, "columns": cols, "preview_rows": preview}


@router.delete("/api/chat/{session_id}")
def clear_chat(session_id: str, user: dict = Depends(get_current_user)):
    """清除指定会话的对话历史（前端点击「新对话」时可调用）。"""
    clear_session(session_id)
    record(A_SESSION_ACTION, user["uid"], user.get("username"),
           {"action": "clear", "session_id": session_id})
    return {"ok": True}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  对话记录持久化 API（会话列表 / 历史详情 / 删除）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/api/sessions")
def api_list_sessions(limit: int = 50, user: dict = Depends(get_current_user)):
    """列出当前用户的历史会话（含标题、最后更新时间、消息条数）。"""
    return db_list_sessions(limit, user["uid"])


@router.get("/api/sessions/{session_id}")
def api_get_session(session_id: str, user: dict = Depends(get_current_user)):
    """获取当前用户指定会话的元信息 + 显示用消息列表。"""
    info = get_session_info(session_id, user["uid"])
    if info is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    info["messages"] = get_display_messages(session_id)
    # Agent 轨迹：按轮次与 assistant 消息一一对应（重开聊天页可回放调用链）
    info["traces"] = load_traces(session_id, user["uid"])
    return info


@router.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: str, user: dict = Depends(get_current_user)):
    """从 SQLite 永久删除当前用户的指定会话及其所有消息。"""
    # 同时清除内存 store（若还在）
    clear_session(session_id)
    _release_session_lock(session_id)
    deleted = db_delete_session(session_id, user["uid"])
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    delete_analysis_records(session_id, user["uid"])
    record(A_SESSION_ACTION, user["uid"], user.get("username"),
           {"action": "delete", "session_id": session_id})
    return {"ok": True}


class RenameRequest(BaseModel):
    title: str


@router.put("/api/sessions/{session_id}")
def api_rename_session(session_id: str, req: RenameRequest, user: dict = Depends(get_current_user)):
    """重命名当前用户的会话标题。"""
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    if len(title) > 60:
        raise HTTPException(status_code=400, detail="标题过长（最多 60 字）")
    if not db_rename_session(session_id, title, user["uid"]):
        raise HTTPException(status_code=404, detail="会话不存在")
    record(A_SESSION_ACTION, user["uid"], user.get("username"),
           {"action": "rename", "session_id": session_id, "title": title})
    return {"ok": True, "session_id": session_id, "title": title}


def _require_db_secret(cfg_id: int, user: dict) -> dict:
    config = get_db_secret(cfg_id, user=user)
    if config is None:
        raise HTTPException(404, "DB config not found")
    return config


@router.get("/api/schema")
def get_schema(db_config_id: int, schema: Optional[str] = None, user: dict = Depends(get_current_user)):
    db_cfg = _require_db_secret(db_config_id, user)
    try:
        engine = build_engine(db_cfg)
        return {"tables": list_tables(engine, schema or db_cfg.get("default_schema"))}
    except Exception as e:
        err = classify_any(e)
        raise HTTPException(status_code=400, detail=f"{err['message']} {err['suggestion']}")
