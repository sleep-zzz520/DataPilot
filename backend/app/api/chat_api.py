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
from app.memory.agent_memory import (
    build_memory_context, extract_and_store_memories, summary_due, generate_summary,
    save_analysis_record, delete_analysis_records,
)
from app.db.schema import list_tables
from app.agent.analysis_plan import AnalysisPlan, make_analysis_plan_graph, PlanRuntime, plan_context
from app.agent.hitl import assess_confirmation
from app.memory import get_history, save_messages, clear_session
from app.memory.store import budget_history, estimate_tokens, MAX_TOKENS
from app.agent.graph import _filter_new_messages
from app.agent.trace import TraceCollector
from app.agent.prompts import SUPERVISOR_PROMPT, CONVERSATION_HINT
# ── 对话记录持久化（SQLite 镜像，不影响现有记忆系统）─────────────────────────
from app.persistence import (
    persist_messages,
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

_CHART_RE = re.compile(r"<!--CHART:(.*?)-->", re.S)
_TABLE_RE = re.compile(r"<!--TABLE:(.*?)-->", re.S)
_IMAGE_RE = re.compile(r"<!--IMAGE_BASE64:(.*?)-->", re.S)

def _build_plan(llm, engine, question: str, schema: Optional[str], file_ids: list, user_id: int):
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
    }, ensure_ascii=False, sort_keys=True)
    return sha256(raw.encode("utf-8")).hexdigest()


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
        return {
            "kind": "ready",
            "plan": AnalysisPlan.model_validate(plan_data),
            "plan_event": {"type": "plan", "event": "plan_resumed", "plan": plan_data},
        }

    plan, plan_event = _build_plan(llm, engine, req.message, schema, req.file_ids, user["uid"])
    file_metadata = [get_upload(str(fid), user["uid"]) or {} for fid in req.file_ids]
    confirmation = assess_confirmation(req.message, plan, file_metadata)
    if not confirmation.get("required"):
        return {"kind": "ready", "plan": plan, "plan_event": plan_event}
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


def _guard_reply(reply: str, plan, validation_status: Optional[str] = None) -> str:
    """质量校验未通过时，把限制明确呈现给用户，避免模型只给确定性结论。"""
    warnings = list(dict.fromkeys(plan.quality_issues))
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


@router.post("/api/chat")
async def chat(req: ChatRequest, user: dict = Depends(get_current_user)):
    sid = req.session_id or str(uuid.uuid4())
    _ensure_session_access(req.session_id, user["uid"])
    lock = await _session_lock(sid)
    async with lock:  # 同一会话串行处理，避免并发交错
        try:
            # ── 同请求防重：30s 内相同请求拒绝，不调 LLM ──────────────────
            dup = _check_duplicate(sid, req.message, req.file_ids, req.approval_id, req.approval_decision)
            if dup:
                return {"reply": None, "chart": None, "table": None, "sql": None, "session_id": sid,
                        "error": {"code": "DUPLICATE_REQUEST", "message": "重复提交",
                                  "suggestion": dup, "severity": "warn"}}
            # ── 预算限流：超限直接拒绝，不调 LLM ──────────────────────────
            ok, reason = check_budget()
            if not ok:
                return {"reply": None, "chart": None, "table": None, "sql": None, "session_id": sid,
                        "error": {"code": "BUDGET_LIMIT", "message": "模型调用预算已用完",
                                  "suggestion": reason, "severity": "block"}}
            llm = build_llm(get_llm_secret(req.llm_config_id))
            db_cfg = get_db_secret(req.db_config_id)
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
            tools = make_tools(engine, db_cfg.get("default_schema"), files=uploaded_files,
                               audit_ctx={"user_id": user["uid"], "username": user.get("username"), "session_id": sid})
            ctx = ""
            for fid in req.file_ids:
                meta = get_upload(str(fid), user["uid"])
                if meta:
                    ctx += f"\n[用户上传文件 {meta['name']}] 列：{meta['columns']} 预览行：{meta['preview_rows']}\n"
            user_text = (ctx + "\n" + req.message) if ctx else req.message
            runtime = PlanRuntime(plan, req.message)
            trace = TraceCollector()
            graph, supervisor_prompt = make_agent(llm, tools, trace=trace, plan_runtime=runtime)

            # ── 多轮对话：先从 SQLite 懒加载（服务重启后自动恢复）───────────────
            _ensure_loaded(sid, user["uid"])
            history = get_history(sid)
            # ── 上下文压缩：token 预算制截断（替代纯轮数截断），
            # 历史 + 当前消息总估算 token 不超过预算，防止上下文无限膨胀 ──
            system_prompt = supervisor_prompt + "\n\n" + CONVERSATION_HINT + "\n\n" + plan_context(plan)
            # ── 记忆注入：长期记忆（跨会话）+ 会话摘要 + 相关历史检索 ──────
            memory_ctx = build_memory_context(user["uid"], sid, req.message)
            if memory_ctx:
                system_prompt += "\n\n" + memory_ctx
            history = budget_history(history, MAX_TOKENS - estimate_tokens(system_prompt))
            input_messages = [
                SystemMessage(content=system_prompt)
            ] + history + [HumanMessage(content=user_text)]

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
            persist_messages(sid, [user_msg] + list(new_msgs), user["uid"])
            # ── Agent 轨迹落库：重开会话可回看工具调用链 ────────────────
            save_trace(sid, trace.snapshot(), user["uid"])

            msgs = result_messages
            visuals, tables, sql = _extract(msgs)
            reply = msgs[-1].content if isinstance(msgs[-1], AIMessage) else ""
            reply = _guard_reply(reply, plan, runtime.validation_status)

            # ── token 用量计量（成本估算 + 预算告警）──────────────────────
            llm_cfg = get_llm_secret(req.llm_config_id)
            in_t, out_t = extract_usage(result_messages)
            if in_t or out_t:
                record_usage(user["uid"], user.get("username"), sid, req.llm_config_id,
                             llm_cfg.get("provider"), llm_cfg.get("model_name"), in_t, out_t)

            # ── 记忆沉淀：节流提取长期事实 + 节流更新会话摘要 ────────────
            extract_and_store_memories(llm, user["uid"], user.get("username"), sid,
                                       req.message, reply)
            if summary_due(sid):
                generate_summary(llm, sid, result_messages)
            save_analysis_record(user["uid"], sid, req.message, plan, plan.evidence, reply)

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
            }
        except Exception as e:
            return {"reply": None, "chart": None, "table": None, "sql": None, "session_id": sid, "error": classify_any(e)}


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
    sid = req.session_id or str(uuid.uuid4())
    _ensure_session_access(req.session_id, user["uid"])
    lock = await _session_lock(sid)

    async def gen():
        async with lock:  # 同一会话串行处理，避免并发交错
            try:
                # ── 同请求防重：30s 内相同请求拒绝，不调 LLM ──────────────
                dup = _check_duplicate(sid, req.message, req.file_ids, req.approval_id, req.approval_decision)
                if dup:
                    yield _sse({"type": "error",
                                "error": {"code": "DUPLICATE_REQUEST", "message": "重复提交",
                                          "suggestion": dup, "severity": "warn"},
                                "session_id": sid})
                    return
                # ── 预算限流：超限直接拒绝，不调 LLM ──────────────────────
                ok, reason = check_budget()
                if not ok:
                    yield _sse({"type": "error",
                                "error": {"code": "BUDGET_LIMIT", "message": "模型调用预算已用完",
                                          "suggestion": reason, "severity": "block"},
                                "session_id": sid})
                    return
                llm = build_llm(get_llm_secret(req.llm_config_id))
                db_cfg = get_db_secret(req.db_config_id)
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
                tools = make_tools(engine, db_cfg.get("default_schema"), files=uploaded_files,
                                   audit_ctx={"user_id": user["uid"], "username": user.get("username"), "session_id": sid})
                ctx = ""
                for fid in req.file_ids:
                    meta = get_upload(str(fid), user["uid"])
                    if meta:
                        ctx += f"\n[用户上传文件 {meta['name']}] 列：{meta['columns']} 预览行：{meta['preview_rows']}\n"
                user_text = (ctx + "\n" + req.message) if ctx else req.message
                plan_updates = []
                runtime = PlanRuntime(plan, req.message, on_update=lambda snapshot: plan_updates.append(snapshot))
                trace = TraceCollector()
                graph, supervisor_prompt = make_agent(llm, tools, trace=trace, plan_runtime=runtime)

                _ensure_loaded(sid, user["uid"])
                history = get_history(sid)
                # ── 上下文压缩：token 预算制截断（与同步接口一致）─────────
                system_prompt = supervisor_prompt + "\n\n" + CONVERSATION_HINT + "\n\n" + plan_context(plan)
                # ── 记忆注入：长期记忆 + 会话摘要 + 相关历史检索（与同步一致）─
                memory_ctx = build_memory_context(user["uid"], sid, req.message)
                if memory_ctx:
                    system_prompt += "\n\n" + memory_ctx
                history = budget_history(history, MAX_TOKENS - estimate_tokens(system_prompt))
                input_messages = [
                    SystemMessage(content=system_prompt)
                ] + history + [HumanMessage(content=user_text)]

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
                        if isinstance(chunk, AIMessageChunk) and chunk.content:
                            yield _sse({"type": "delta", "text": chunk.content})
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
                persist_messages(sid, [user_msg] + list(all_new), user["uid"])
                # ── Agent 轨迹落库：重开会话可回看工具调用链 ────────────
                save_trace(sid, trace.snapshot(), user["uid"])

                # ── 提取图表/SQL，发送最终结果 ────────────────────────────
                result_messages = list(input_messages) + list(all_new)
                runtime.finalize()
                visuals, tables, sql = _extract(result_messages)

                # ── token 用量计量（成本估算 + 预算告警）──────────────────
                llm_cfg = get_llm_secret(req.llm_config_id)
                in_t, out_t = extract_usage(result_messages)
                if in_t or out_t:
                    record_usage(user["uid"], user.get("username"), sid, req.llm_config_id,
                                 llm_cfg.get("provider"), llm_cfg.get("model_name"), in_t, out_t)

                reply = ""
                for m in reversed(result_messages):
                    if isinstance(m, AIMessage) and m.content:
                        reply = m.content
                        break
                reply = _guard_reply(reply, plan, runtime.validation_status)

                # ── 记忆沉淀：节流提取长期事实 + 节流更新会话摘要 ──────────
                extract_and_store_memories(llm, user["uid"], user.get("username"), sid,
                                           req.message, reply)
                if summary_due(sid):
                    generate_summary(llm, sid, result_messages)
                save_analysis_record(user["uid"], sid, req.message, plan, plan.evidence, reply)

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
                })
            except Exception as e:
                yield _sse({"type": "error", "error": classify_any(e), "session_id": sid})

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


@router.get("/api/schema")
def get_schema(db_config_id: int, schema: Optional[str] = None):
    try:
        db_cfg = get_db_secret(db_config_id)
        engine = build_engine(db_cfg)
        return {"tables": list_tables(engine, schema or db_cfg.get("default_schema"))}
    except Exception as e:
        err = classify_any(e)
        raise HTTPException(status_code=400, detail=f"{err['message']} {err['suggestion']}")
