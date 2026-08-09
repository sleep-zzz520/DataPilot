"""对话记录 SQLite 持久化模块。

职责：
  - 镜像内存 ConversationStore 的消息到 SQLite（重启不丢）
  - 提供会话列表 / 会话详情 / 删除接口
  - 不做任何摘要、检索、压缩

用法（在现有 chat 流程中两行即可接入）：
    from app.persistence import persist_messages, load_session, list_sessions

    # 每轮对话结束后追加保存（幂等，只写新增消息）
    persist_messages(session_id, full_history)

    # 恢复会话时加载完整 LangChain 消息
    messages = load_session(session_id)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    ToolMessage,
    SystemMessage,
)

# ── 数据库路径（与 data/llm_configs.json 同级）────────────────────────────────
# 用 __file__ 推导绝对路径，避免从不同工作目录启动时数据分裂
_BASE_DIR = Path(__file__).resolve().parents[2]  # backend/
_DB_DIR = _BASE_DIR / "data"
_DB_PATH = _DB_DIR / "chat_history.db"
_lock = threading.Lock()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  内部：连接 & 建表
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _conn() -> sqlite3.Connection:
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id         TEXT PRIMARY KEY,
            title      TEXT    DEFAULT '新对话',
            created_at TEXT    DEFAULT (datetime('now','localtime')),
            updated_at TEXT    DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            seq        INTEGER NOT NULL,   -- 消息序号（从 0 开始）
            role       TEXT    NOT NULL,
            content    TEXT,
            extra      TEXT,               -- JSON：tool_calls / tool_call_id / name 等
            UNIQUE(session_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, seq);
        CREATE TABLE IF NOT EXISTS uploads (
            file_id      TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            path         TEXT NOT NULL,
            columns      TEXT,               -- JSON：列名列表
            preview_rows TEXT,               -- JSON：前 5 行预览
            created_at   TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS traces (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            turn_seq   INTEGER NOT NULL,      -- 该会话内轮次序号（与消息轮次对齐）
            data       TEXT    NOT NULL,      -- JSON：该轮完整轨迹数组
            UNIQUE(session_id, turn_seq)
        );
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id            TEXT PRIMARY KEY,
            user_id       INTEGER NOT NULL,
            session_id    TEXT NOT NULL,
            request_hash  TEXT NOT NULL,
            payload       TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT DEFAULT (datetime('now','localtime')),
            expires_at    TEXT NOT NULL,
            decided_at    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pending_approval_owner
            ON pending_approvals(user_id, session_id, status);
    """)
    # 迁移：旧库为 sessions/uploads 补充 user_id 列（多租户隔离）
    for table in ("sessions", "uploads"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if "user_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
    # 迁移：users 表补充 role 列（admin / user）
    ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "role" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
    conn.commit()
    return conn


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  序列化 / 反序列化  LangChain Message ↔ (role, content, extra)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _ser(msg) -> tuple[str, str, Optional[str]]:
    """LangChain Message → (role, content, extra_json?)"""
    extra: dict = {}
    # 稳定 ID：持久化去重的可靠依据（内容可能相同，如两轮 tool 都返回 'ok'）
    mid = getattr(msg, "id", None)
    if mid:
        extra["msg_id"] = mid
    if isinstance(msg, HumanMessage):
        role = "user"
    elif isinstance(msg, AIMessage):
        role = "assistant"
        tc = getattr(msg, "tool_calls", None)
        if tc:
            extra["tool_calls"] = tc
    elif isinstance(msg, ToolMessage):
        role = "tool"
        if getattr(msg, "tool_call_id", None):
            extra["tool_call_id"] = msg.tool_call_id
        if getattr(msg, "name", None):
            extra["name"] = msg.name
    elif isinstance(msg, SystemMessage):
        role = "system"
    else:
        role = "unknown"
    return role, (msg.content or ""), (json.dumps(extra, ensure_ascii=False) if extra else None)


def _deser(role: str, content: str, extra_str: Optional[str]):
    """(role, content, extra_json?) → LangChain Message 实例"""
    extra: dict = json.loads(extra_str) if extra_str else {}
    mid = extra.get("msg_id")
    if role == "user":
        return HumanMessage(content=content, id=mid)
    if role == "assistant":
        tc = extra.get("tool_calls") or []
        return AIMessage(content=content, tool_calls=tc, id=mid)
    if role == "tool":
        return ToolMessage(
            content=content,
            tool_call_id=extra.get("tool_call_id", ""),
            name=extra.get("name"),
            id=mid,
        )
    if role == "system":
        return SystemMessage(content=content, id=mid)
    return HumanMessage(content=f"[{role}] {content}", id=mid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  公开 API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def persist_messages(session_id: str, all_messages: list, user_id: Optional[int] = None) -> None:
    """将 all_messages 中尚未落盘的消息追加写入 SQLite（幂等）。

    用法：每轮对话结束后把内存 store 的完整历史传进来；
    函数按消息内容指纹去重，只 INSERT 新增部分，并保证 seq 单调递增。

    相比按 COUNT(*) 切片定位增量，本实现不受内存历史被裁剪
    （MAX_TURNS 截断）或并发写入的影响，不会丢消息、也不会 seq 冲突。
    """
    with _lock:
        conn = _conn()
        try:
            # 确保 session 记录存在
            row = conn.execute(
                "SELECT id FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                title = _auto_title(all_messages)
                conn.execute(
                    "INSERT INTO sessions(id, title, user_id) VALUES (?,?,?)",
                    (session_id, title, user_id),
                )

            # 已落盘消息：优先按 msg_id 去重，旧数据（无 id）回退到内容指纹
            exist_rows = conn.execute(
                "SELECT role, content, extra FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchall()
            exist_ids: set = set()
            exist_fps: set = set()
            for r in exist_rows:
                ex = json.loads(r["extra"]) if r["extra"] else {}
                mid = ex.get("msg_id")
                if mid:
                    exist_ids.add(mid)
                else:
                    exist_fps.add(_fingerprint(r["role"], r["content"], r["extra"]))
            max_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]

            # 找出真正新增的消息（保持原有顺序）
            new_msgs: list = []
            for m in all_messages:
                role, content, extra_str = _ser(m)
                ex = json.loads(extra_str) if extra_str else {}
                mid = ex.get("msg_id")
                if mid:
                    if mid in exist_ids:
                        continue
                    exist_ids.add(mid)
                else:
                    fp = _fingerprint(role, content, extra_str)
                    if fp in exist_fps:
                        continue
                    exist_fps.add(fp)
                new_msgs.append(m)
            next_seq = max_seq + 1
            for i, msg in enumerate(new_msgs):
                role, content, extra = _ser(msg)
                conn.execute(
                    "INSERT INTO messages(session_id, seq, role, content, extra) "
                    "VALUES (?,?,?,?,?)",
                    (session_id, next_seq + i, role, content, extra),
                )

            # 更新 title（首次保存时）和时间戳
            updates: dict = {"updated_at": datetime.now().isoformat(timespec="seconds")}
            if row is None:
                updates["title"] = _auto_title(all_messages)
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE sessions SET {sets} WHERE id=?",
                list(updates.values()) + [session_id],
            )
            conn.commit()
        finally:
            conn.close()


def _fingerprint(role: str, content: str, extra: Optional[str]) -> str:
    """消息内容指纹：role + content + extra 的稳定摘要（用于幂等去重）。"""
    import hashlib
    key = f"{role}\x00{content or ''}\x00{extra or ''}"
    return hashlib.md5(key.encode("utf-8", errors="replace")).hexdigest()


def load_session(session_id: str, user_id: Optional[int] = None) -> list:
    """从 SQLite 加载完整 LangChain Message 列表（用于恢复内存 store）。

    user_id 归属校验走 sessions 表（messages 表本身没有 user_id 列）。
    """
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT m.role, m.content, m.extra FROM messages m "
                "JOIN sessions s ON s.id = m.session_id "
                "WHERE m.session_id=? AND s.user_id IS ? ORDER BY m.seq",
                (session_id, user_id),
            ).fetchall()
            return [_deser(r["role"], r["content"], r["extra"]) for r in rows]
        finally:
            conn.close()


def save_trace(session_id: str, entries: list, user_id: Optional[int] = None) -> bool:
    """把一轮对话的 Agent 轨迹写入 SQLite（按轮次 upsert，幂等）。

    轮次序号取"该会话当前用户消息数 - 1"，与 get_display_messages 的轮次
    （每轮一条 user + 一条 assistant）对齐，重开会话可原样回放。
    """
    with _lock:
        conn = _conn()
        try:
            # 多租户隔离：会话不存在或不属于该用户则不写入
            row = conn.execute(
                "SELECT id FROM sessions WHERE id=? AND user_id IS ?",
                (session_id, user_id),
            ).fetchone()
            if row is None:
                return False
            turn_seq = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='user'",
                (session_id,),
            ).fetchone()[0] - 1
            if turn_seq < 0:
                return False
            conn.execute(
                "INSERT OR REPLACE INTO traces(session_id, turn_seq, data) VALUES (?,?,?)",
                (session_id, turn_seq, json.dumps(entries, ensure_ascii=False)),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def load_traces(session_id: str, user_id: Optional[int] = None) -> List[List[Dict[str, Any]]]:
    """按轮次顺序加载该会话的 Agent 轨迹（每轮一个数组，可能为空数组）。"""
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT t.data FROM traces t "
                "JOIN sessions s ON s.id = t.session_id "
                "WHERE t.session_id=? AND s.user_id IS ? "
                "ORDER BY t.turn_seq",
                (session_id, user_id),
            ).fetchall()
            return [json.loads(r["data"]) for r in rows]
        finally:
            conn.close()


def list_sessions(limit: int = 50, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """返回当前用户的最近会话列表（含标题、更新时间、消息条数）。"""
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT s.id, s.title, s.created_at, s.updated_at, "
                "       COUNT(m.id) AS msg_count "
                "FROM sessions s "
                "LEFT JOIN messages m ON m.session_id = s.id "
                "WHERE s.user_id IS ? "
                "GROUP BY s.id "
                "ORDER BY s.updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def delete_session(session_id: str, user_id: Optional[int] = None) -> bool:
    """删除当前用户的会话及其所有消息，返回是否实际删除了记录。"""
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "DELETE FROM sessions WHERE id=? AND user_id IS ?",
                (session_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def rename_session(session_id: str, title: str, user_id: Optional[int] = None) -> bool:
    """重命名当前用户的会话标题，返回是否实际更新了记录。"""
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=? AND user_id IS ?",
                (title, datetime.now().isoformat(timespec="seconds"), session_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def save_pending_approval(approval_id: str, user_id: int, session_id: str,
                          request_hash: str, payload: dict, ttl_seconds: int = 900) -> None:
    """保存一次性确认请求；payload 只保存计划和展示所需元数据，不保存密钥。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO pending_approvals "
                "(id, user_id, session_id, request_hash, payload, expires_at) "
                "VALUES (?,?,?,?,?,datetime('now', ?))",
                (approval_id, user_id, session_id, request_hash,
                 json.dumps(payload, ensure_ascii=False), f"+{int(ttl_seconds)} seconds"),
            )
            conn.commit()
        finally:
            conn.close()


def consume_pending_approval(approval_id: str, user_id: int, session_id: str,
                             request_hash: str, decision: str) -> dict:
    """原子消费确认请求，返回 approved/rejected/invalid，防重放和跨用户使用。"""
    if decision not in ("approve", "reject"):
        return {"status": "invalid", "reason": "不支持的确认操作"}
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT request_hash, payload FROM pending_approvals "
                "WHERE id=? AND user_id=? AND session_id=? AND status='pending' "
                "AND expires_at > datetime('now')",
                (approval_id, user_id, session_id),
            ).fetchone()
            if row is None:
                return {"status": "invalid", "reason": "确认请求不存在、已过期或已处理"}
            if row["request_hash"] != request_hash:
                return {"status": "invalid", "reason": "确认请求与原始分析不匹配"}
            status = "approved" if decision == "approve" else "rejected"
            cur = conn.execute(
                "UPDATE pending_approvals SET status=?, decided_at=datetime('now') "
                "WHERE id=? AND status='pending'",
                (status, approval_id),
            )
            if cur.rowcount != 1:
                return {"status": "invalid", "reason": "确认请求已被处理"}
            conn.commit()
            return {"status": status, "payload": json.loads(row["payload"])}
        finally:
            conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  用户账号（开放注册 + 多租户）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def create_user(username: str, password_hash: str) -> Optional[dict]:
    """创建用户；第一个注册的账号为 admin，其余为 user。用户名重复返回 None。"""
    with _lock:
        conn = _conn()
        try:
            is_first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            role = "admin" if is_first else "user"
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?,?,?)",
                (username, password_hash, role),
            )
            conn.commit()
            return {"id": cur.lastrowid, "role": role}
        except sqlite3.IntegrityError:
            return None
        finally:
            conn.close()


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT id, username, password_hash, role, created_at FROM users WHERE username=?",
                (username,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def claim_orphan_data(user_id: int) -> None:
    """把旧版无主数据（user_id 为 NULL 的会话/上传）归给指定用户。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE sessions SET user_id=? WHERE user_id IS NULL", (user_id,))
            conn.execute("UPDATE uploads SET user_id=? WHERE user_id IS NULL", (user_id,))
            conn.commit()
        finally:
            conn.close()


def list_users() -> List[Dict[str, Any]]:
    """用户列表（不含密码哈希）。"""
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT id, username, role, created_at FROM users ORDER BY id",
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def update_user_role(user_id: int, role: str) -> bool:
    """设置用户角色（admin/user），返回是否更新了记录。"""
    if role not in ("admin", "user"):
        return False
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def update_password(user_id: int, password_hash: str) -> bool:
    """更新用户密码哈希。"""
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def delete_user(user_id: int) -> list:
    """注销账号：删除用户及其会话/上传记录，返回需删除的上传文件磁盘路径。"""
    with _lock:
        conn = _conn()
        try:
            paths = [r[0] for r in conn.execute(
                "SELECT path FROM uploads WHERE user_id=?", (user_id,)
            ).fetchall()]
            conn.execute("DELETE FROM uploads WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))  # messages 级联删除
            cur = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()
            return paths if cur.rowcount else []
        finally:
            conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  上传文件元信息（文件本体落盘在 data/uploads/，这里只存元信息）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def save_upload(file_id: str, name: str, path: str, columns: list, preview_rows: list, user_id: Optional[int] = None) -> None:
    """记录一次上传的文件元信息。"""
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO uploads(file_id, name, path, columns, preview_rows, user_id) "
                "VALUES (?,?,?,?,?,?)",
                (file_id, name, path,
                 json.dumps(columns, ensure_ascii=False),
                 json.dumps(preview_rows, ensure_ascii=False, default=str),
                 user_id),
            )
            conn.commit()
        finally:
            conn.close()


def get_upload(file_id: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """按 file_id 读取当前用户的上传文件元信息；不存在返回 None。"""
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT file_id, name, path, columns, preview_rows FROM uploads "
                "WHERE file_id=? AND user_id IS ?",
                (file_id, user_id),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["columns"] = json.loads(d["columns"]) if d["columns"] else []
            d["preview_rows"] = json.loads(d["preview_rows"]) if d["preview_rows"] else []
            return d
        finally:
            conn.close()


def get_display_messages(session_id: str) -> List[Dict[str, Any]]:
    """返回前端可直接渲染的消息列表（已过滤 system/tool 中间消息）。

    按"轮次"聚合：一轮 = user 消息 + 该轮最终 assistant 回复。
    中间过程消息（assistant 的 tool_calls、tool 结果）被隐藏，
    其 sql / chart / table 合并到该轮最终回复上，保证顺序与实时对话一致
    （不会出现"图在上、话在下"）。

    返回格式：
    [
      {"role": "user",      "text": "...", "sql": null},
      {"role": "assistant", "text": "...", "sql": "SELECT ...", "chart": {...}, "table": {...}},
      ...
    ]
    """
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT role, content, extra FROM messages "
                "WHERE session_id=? ORDER BY seq",
                (session_id,),
            ).fetchall()

            import re
            _CHART_RE = re.compile(r"<!--CHART:(.*?)-->", re.S)
            _TABLE_RE = re.compile(r"<!--TABLE:(.*?)-->", re.S)
            _IMAGE_RE = re.compile(r"<!--IMAGE_BASE64:(.*?)-->", re.S)

            # ── 第一遍：按轮次聚合 ────────────────────────────────────────
            turns: List[Dict[str, Any]] = []
            cur: Optional[Dict[str, Any]] = None
            for r in rows:
                role, content, extra_str = r["role"], r["content"], r["extra"]
                extra: dict = json.loads(extra_str) if extra_str else {}

                if role == "user":
                    cur = {
                        "user_text": content,
                        "assistant": None,   # 该轮最终回复（若有）
                        "sql": None,         # 该轮最后一次查询 SQL
                        "visuals": [],       # [{chart, image}] 该轮所有图表
                        "tables": [],        # 该轮所有表格
                    }
                    turns.append(cur)

                elif role == "assistant":
                    if cur is None:  # 极端情况：以 assistant 开头
                        cur = {"user_text": "", "assistant": None,
                               "sql": None, "visuals": [], "tables": []}
                        turns.append(cur)
                    # 始终保留该轮"最后一条" assistant（即最终回复）
                    entry: Dict[str, Any] = {"text": content or "", "sql": None}
                    for tc in extra.get("tool_calls", []):
                        if tc.get("name") in ("query_mysql", "run_sql") and tc.get("args"):
                            entry["sql"] = tc["args"].get("sql")
                    cur["assistant"] = entry
                    # 新一轮 tool 结果开始前，重置该轮附属数据
                    cur["sql"] = None

                elif role == "tool" and content:
                    if cur is None:
                        continue
                    item: dict = {}
                    mc = _CHART_RE.search(content)
                    if mc:
                        try:
                            item["chart"] = json.loads(mc.group(1))
                        except Exception:
                            pass
                    mi = _IMAGE_RE.search(content)
                    if mi:
                        item["image"] = mi.group(1)
                    if item:
                        cur["visuals"].append(item)
                    for mt in _TABLE_RE.finditer(content):
                        try:
                            cur["tables"].append(json.loads(mt.group(1)))
                        except Exception:
                            pass

            # ── 第二遍：组装输出 ──────────────────────────────────────────
            result: List[Dict[str, Any]] = []
            for t in turns:
                if t["user_text"]:
                    result.append({"role": "user", "text": t["user_text"]})
                a = t["assistant"]
                if a is None:
                    # 该轮只有 tool 结果没有最终回复（异常中断）：合并成一条空消息兜底
                    if t["visuals"] or t["tables"]:
                        a = {"text": "", "sql": None}
                    else:
                        continue
                entry: Dict[str, Any] = {
                    "role": "assistant",
                    "text": a["text"],
                    "sql": a["sql"] or t["sql"],
                }
                if t["visuals"]:
                    entry["visuals"] = t["visuals"]
                if t["tables"]:
                    entry["tables"] = t["tables"]
                result.append(entry)

            return result
        finally:
            conn.close()


def get_session_info(session_id: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """查询会话元信息。user_id 为 None 时做存在性检查；指定 user_id 时校验归属。"""
    with _lock:
        conn = _conn()
        try:
            if user_id is None:
                row = conn.execute(
                    "SELECT id, title, created_at, updated_at FROM sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, title, created_at, updated_at FROM sessions "
                    "WHERE id=? AND user_id IS ?",
                    (session_id, user_id),
                ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  工具函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _auto_title(messages: list) -> str:
    """用第一条用户消息的前 30 字作为会话标题。"""
    for m in messages:
        if isinstance(m, HumanMessage) and m.content:
            txt = m.content.strip().replace("\n", " ")
            return txt[:30] + ("..." if len(txt) > 30 else "")
    return "新对话"
