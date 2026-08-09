"""审计日志（合规留痕）：SQL 查询 / 配置变更 / 用户操作 的只追加记录。

设计原则：
- 独立表 audit_logs（与业务库同库，但只提供 INSERT + SELECT，应用层无修改/删除入口）
- detail 只记业务元信息，绝不落明文密钥/密码（合规）
- 通过 `from app import persistence` 动态取 _DB_PATH，保证测试隔离（conftest monkeypatch）生效
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

from app import persistence

_lock = threading.Lock()

_CREATE = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    DEFAULT (datetime('now','localtime')),
    user_id    INTEGER,
    username   TEXT,
    action     TEXT    NOT NULL,
    detail     TEXT,               -- JSON
    ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
"""

# 动作类型常量（便于各接入点统一、查询过滤）
A_SQL_QUERY = "sql_query"            # SQL 执行（含上传文件查询）
A_CONFIG_CHANGE = "config_change"    # LLM/DB 配置 新增/修改/删除
A_USER_ACTION = "user_action"        # 注册/登录/改密/注销/角色变更
A_SESSION_ACTION = "session_action"  # 会话删除/重命名/清空
A_FILE_UPLOAD = "file_upload"        # 上传文件
A_HITL_CONFIRMATION = "hitl_confirmation"  # 高风险分析确认/拒绝


def _connect() -> sqlite3.Connection:
    persistence._DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(persistence._DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_CREATE)
    conn.commit()
    return conn


def record(action: str, user_id: Optional[int] = None, username: Optional[str] = None,
           detail: Optional[dict] = None, ip: Optional[str] = None) -> None:
    """追加一条审计记录。detail 中请勿放入明文密钥/密码。"""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO audit_logs(user_id, username, action, detail, ip) VALUES (?,?,?,?,?)",
                (user_id, username, action,
                 json.dumps(detail or {}, ensure_ascii=False, default=str), ip),
            )
            conn.commit()
        finally:
            conn.close()


def list_audit(limit: int = 100, action: Optional[str] = None,
               username: Optional[str] = None) -> list[dict]:
    """倒序查询审计记录（合规查看）。limit 上限 500 防滥用。"""
    limit = max(1, min(limit, 500))
    sql = "SELECT id, ts, user_id, username, action, detail, ip FROM audit_logs WHERE 1=1"
    params: list = []
    if action:
        sql += " AND action=?"
        params.append(action)
    if username:
        sql += " AND username=?"
        params.append(username)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
                except (json.JSONDecodeError, TypeError):
                    d["detail"] = {}
                out.append(d)
            return out
        finally:
            conn.close()


def count_by_action(limit_hours: int = 24) -> dict:
    """最近 N 小时各动作数量（供合规/告警概览）。"""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT action, COUNT(*) AS n FROM audit_logs "
                "WHERE ts >= datetime('now', ?) GROUP BY action ORDER BY n DESC",
                (f"-{int(limit_hours)} hours",),
            ).fetchall()
            return {r["action"]: r["n"] for r in rows}
        finally:
            conn.close()
