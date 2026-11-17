"""测试账号合并保持数据、历史归因与重复执行的一致性。"""
import sqlite3

import pytest

from app.auth import verify_password
from scripts.maintain_test_account import consolidate, inspect_accounts


def make_database(tmp_path):
    path = tmp_path / "accounts.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, role TEXT, password_hash TEXT);
            INSERT INTO users VALUES (1,'test_aaaaaaaaaaaaaaaa','admin','old');
            INSERT INTO users VALUES (2,'test_bbbbbbbbbbbbbbbb','user','old');
            INSERT INTO users VALUES (3,'analyst','user','old');
            CREATE TABLE sessions(id TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id));
            INSERT INTO sessions VALUES ('old-session',2);
            CREATE TABLE audit_logs(user_id INTEGER,username TEXT,action TEXT,detail TEXT);
            INSERT INTO audit_logs VALUES (2,'test_bbbbbbbbbbbbbbbb','login','{}');
        """)
    return path


def test_consolidation_preserves_data_history_and_existing_user_id(tmp_path):
    path = make_database(tmp_path)
    result = consolidate(path)
    assert result["kept_user_id"] == 1
    assert result["removed_accounts"] == 1
    assert result["moved_rows"]["sessions"] == 1
    assert inspect_accounts(path)["users"] == [
        {"id": 1, "username": "admin", "role": "admin"},
        {"id": 3, "username": "analyst", "role": "user"},
    ]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT user_id FROM sessions").fetchone() == (1,)
        assert conn.execute("SELECT user_id,username FROM audit_logs LIMIT 1").fetchone() == (2, 'test_bbbbbbbbbbbbbbbb')
        assert verify_password("admin1234", conn.execute("SELECT password_hash FROM users WHERE id=1").fetchone()[0])
    with sqlite3.connect(result["backup"]) as backup:
        assert backup.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
        assert backup.execute("SELECT user_id FROM sessions").fetchone() == (2,)
    assert consolidate(path) == {"changed": False, "kept_user_id": 1, "removed_accounts": 0}


def test_inspection_does_not_modify_accounts(tmp_path):
    path = make_database(tmp_path)
    original = path.read_bytes()
    assert len(inspect_accounts(path)["users"]) == 3
    assert path.read_bytes() == original
    assert not (tmp_path / "backups").exists()


def test_conflicting_data_rolls_back_without_losing_accounts_or_memories(tmp_path):
    path = make_database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE user_memories(user_id INTEGER,key TEXT,value TEXT,UNIQUE(user_id,key));
            INSERT INTO user_memories VALUES (1,'format','table'),(2,'format','chart');
        """)
    with pytest.raises(sqlite3.IntegrityError):
        consolidate(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
        assert conn.execute("SELECT user_id FROM sessions").fetchone() == (2,)
        assert conn.execute("SELECT COUNT(*) FROM user_memories").fetchone()[0] == 2


def test_no_existing_test_account_is_not_created(tmp_path):
    path = make_database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE users SET username='user_' || id")
    with pytest.raises(ValueError, match="不会自动注册"):
        consolidate(path)
    assert len(inspect_accounts(path)["users"]) == 3
