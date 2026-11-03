"""同请求防重（chat_api._check_duplicate）单测。"""
import time

import pytest

from app.api import chat_api
from app.api.chat_api import _check_duplicate, _DEDUP_WINDOW_SECONDS


@pytest.fixture(autouse=True)
def _clean_dedup():
    """每个测试前清空防重表，避免跨测试污染。"""
    chat_api._dedup.clear()
    yield
    chat_api._dedup.clear()


def test_first_request_allowed():
    assert _check_duplicate("s1", "你好", []) is None


def test_same_request_within_window_rejected():
    _check_duplicate("s1", "你好", [])
    hint = _check_duplicate("s1", "你好", [])
    assert hint and "重复提交" in hint


def test_different_session_allowed():
    _check_duplicate("s1", "你好", [])
    assert _check_duplicate("s2", "你好", []) is None


def test_different_message_allowed():
    _check_duplicate("s1", "你好", [])
    assert _check_duplicate("s1", "你好吗", []) is None


def test_different_file_ids_allowed():
    _check_duplicate("s1", "你好", ["f1"])
    assert _check_duplicate("s1", "你好", ["f2"]) is None
    assert _check_duplicate("s1", "你好", []) is None  # 无文件 ≠ 有文件


def test_whitespace_normalized():
    _check_duplicate("s1", "  你好  ", [])
    assert _check_duplicate("s1", "你好", []) is not None  # 首尾空格视为相同


def test_expired_entry_cleared():
    _check_duplicate("s1", "你好", [])
    # 把该键的时间戳改成过期
    key = ("s1", "你好", ())
    chat_api._dedup[key] = time.time() - _DEDUP_WINDOW_SECONDS - 1
    assert _check_duplicate("s1", "你好", []) is None  # 过期 → 放行并重新记录
    assert len(chat_api._dedup) == 1


def test_expired_entries_are_cleaned_up():
    _check_duplicate("s1", "a", [])
    _check_duplicate("s2", "b", [])
    for k in list(chat_api._dedup):
        chat_api._dedup[k] = time.time() - _DEDUP_WINDOW_SECONDS - 1
    _check_duplicate("s3", "c", [])  # 触发清理
    assert len(chat_api._dedup) == 1  # 只剩 s3/c 的新记录


# ── 接口级：重复请求被拒绝且不调 LLM ────────────────────────────────────────
def test_stream_duplicate_returns_error(client, monkeypatch):
    from app.api import chat_api as chat_mod
    called = {"n": 0}
    # ACL is exercised with two real fixture databases in test_db_access.py.
    monkeypatch.setattr(chat_mod, "_require_db_secret", lambda cfg_id, user: {})
    monkeypatch.setattr(chat_mod, "build_llm", lambda *a, **k: called.update(n=called["n"] + 1) or object())
    token = _register(client, "alice")
    h = {"Authorization": f"Bearer {token}"}
    payload = {"message": "重复消息", "session_id": "dup-test-s", "llm_config_id": 1, "db_config_id": 1}
    chat_mod._dedup.clear()
    try:
        r1 = client.post("/api/chat/stream", json=payload, headers=h)
        # 30s 内第二次相同请求 → 直接 error 事件，不调 LLM
        r2 = client.post("/api/chat/stream", json=payload, headers=h)
        assert r2.status_code == 200
        assert "DUPLICATE_REQUEST" in r2.text
        assert called["n"] == 1  # build_llm 只被第一次请求调用，第二次被防重拦截
    finally:
        chat_mod._dedup.clear()


def _register(client, name, pwd="pass1234"):
    return client.post("/api/auth/register", json={"username": name, "password": pwd}).json()["token"]
