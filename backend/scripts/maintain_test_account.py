"""手动检查或合并演示测试账号；不会在启动或登录时创建账号。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth import hash_password, verify_password

TEST_NAME = re.compile(r"test_[0-9a-f]{16}\Z")
HISTORY_TABLES = {"audit_logs", "usage_logs"}
OWNER_TABLES = {
    "sessions", "uploads", "user_memories", "pending_approvals",
    "analysis_artifacts", "artifact_runs", "async_tasks", "projects",
    "analysis_records", "message_feedback", "verified_queries", "query_logs",
}


def inspect_accounts(database: Path) -> dict:
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        users = [dict(zip(("id", "username", "role"), row)) for row in
                 conn.execute("SELECT id, username, role FROM users ORDER BY id")]
        owners = {}
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table in OWNER_TABLES:
                owners[table] = conn.execute(
                    f'SELECT user_id, COUNT(*) FROM "{table}" GROUP BY user_id'
                ).fetchall()
        return {"users": users, "data_by_owner": owners}


def consolidate(database: Path, additional_test_users=()) -> dict:
    database = database.resolve()
    conn = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=30)
    backup_path = None
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        users = conn.execute("SELECT id, username, role, password_hash FROM users ORDER BY id").fetchall()
        candidates = [u for u in users if u[1] == "admin" or TEST_NAME.fullmatch(u[1])
                      or u[1] in additional_test_users]
        if not candidates:
            raise ValueError("没有已有测试账号；本命令不会自动注册账号")
        keeper = next((u for u in candidates if u[1] == "admin"), None)
        keeper = keeper or next((u for u in candidates if u[2] == "admin"), candidates[0])
        removed_ids = [u[0] for u in candidates if u[0] != keeper[0]]
        if not removed_ids and keeper[1:3] == ("admin", "admin") and verify_password("admin1234", keeper[3]):
            conn.rollback()
            return {"changed": False, "kept_user_id": keeper[0], "removed_accounts": 0}

        # SQLite 在线备份包含 WAL；在写入前备份，备份目录仅当前用户可读。
        backup_dir = database.parent / "backups"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        backup_path = backup_dir / ("test-accounts-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup_path) as backup:
                source.backup(backup)
        backup_path.chmod(0o600)

        moved = {}
        if removed_ids:
            placeholders = ",".join("?" for _ in removed_ids)
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                if "user_id" not in columns or table in HISTORY_TABLES:
                    continue
                if table not in OWNER_TABLES:
                    raise ValueError(f"未识别的用户数据表 {table}，已中止合并")
                if table == "async_tasks" and conn.execute(
                    f"SELECT 1 FROM async_tasks WHERE user_id IN ({placeholders}) "
                    "AND status IN ('pending','queued','running') LIMIT 1", removed_ids
                ).fetchone():
                    raise ValueError("测试账号仍有运行中任务，已中止合并")
                # 唯一约束冲突时整笔事务回滚，避免丢弃任何记忆或查询记录。
                cur = conn.execute(
                    f'UPDATE "{table}" SET user_id=? WHERE user_id IN ({placeholders})',
                    [keeper[0], *removed_ids],
                )
                moved[table] = cur.rowcount
                if "username" in columns:
                    conn.execute(f'UPDATE "{table}" SET username=? WHERE user_id=?', ("admin", keeper[0]))
            if "pending_approvals" in tables:
                conn.execute(
                    "UPDATE pending_approvals SET status='expired' WHERE user_id=? AND status='pending'",
                    (keeper[0],),
                )
            conn.execute(f"DELETE FROM users WHERE id IN ({placeholders})", removed_ids)

        conn.execute("UPDATE users SET username=?, role='admin', password_hash=? WHERE id=?",
                     ("admin", hash_password("admin1234"), keeper[0]))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='audit_logs'").fetchone():
            conn.execute(
                "INSERT INTO audit_logs(user_id, username, action, detail) VALUES (?, ?, ?, ?)",
                (keeper[0], "admin", "user_action", json.dumps({
                    "action": "consolidate_test_accounts", "removed_user_ids": removed_ids,
                    "previous_username": keeper[1], "moved_rows": moved,
                }, ensure_ascii=False)),
            )
        conn.commit()
        return {"changed": True, "kept_user_id": keeper[0], "removed_accounts": len(removed_ids),
                "moved_rows": moved, "backup": str(backup_path)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--action", choices=("inspect", "consolidate"), default="inspect")
    parser.add_argument("--include-user", action="append", default=[],
                        help="明确指定额外需要合并的旧测试账号，可重复传入")
    args = parser.parse_args()
    if args.action == "consolidate":
        print(json.dumps(consolidate(args.database, args.include_user), ensure_ascii=False))
    print(json.dumps(inspect_accounts(args.database), ensure_ascii=False))


if __name__ == "__main__":
    main()
