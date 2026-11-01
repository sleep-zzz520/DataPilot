"""会话内存存储（memory/store.py）：LRU / TTL / 轮次裁剪 单测。"""
import time

import pytest
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage

from app.memory.store import ConversationStore


def _mk(session="s1"):
    return [
        HumanMessage(content="你好"),
        AIMessage(content="你好！"),
    ]


# ── 基本存取 ──────────────────────────────────────────────────────────────────
def test_save_and_get():
    s = ConversationStore()
    s.save("a", _mk())
    msgs = s.get("a")
    assert len(msgs) == 2 and msgs[0].content == "你好"


def test_get_unknown_session_returns_empty():
    assert ConversationStore().get("nope") == []


def test_get_returns_copy():
    s = ConversationStore()
    s.save("a", _mk())
    msgs = s.get("a")
    msgs.append(HumanMessage(content="篡改"))
    assert len(s.get("a")) == 2  # 内部不受影响


# ── 轮次裁剪 ──────────────────────────────────────────────────────────────────
def test_trim_excess_turns():
    s = ConversationStore(max_turns=2)
    for i in range(4):  # 4 轮 user+ai
        s.save("a", [HumanMessage(content=f"q{i}"), AIMessage(content=f"a{i}")])
    msgs = s.get("a")
    assert sum(isinstance(m, HumanMessage) for m in msgs) == 2
    assert msgs[0].content == "q2"  # 保留的是后两轮


def test_trim_by_token_budget():
    # 小预算 → 按 token 裁剪（头部被裁，保留最近的）
    s = ConversationStore(max_turns=200, max_tokens=35)
    for i in range(4):
        s.save("a", [HumanMessage(content=f"问题{i}"), AIMessage(content=f"回答{i}")])
    msgs = s.get("a")
    assert msgs[-1].content == "回答3"  # 保留最近的
    assert len(msgs) < 8  # 预算 10 token 装不下全部


def test_trim_token_budget_is_strict():
    s = ConversationStore(max_turns=200, max_tokens=1)
    s.save("a", [HumanMessage(content="x"), AIMessage(content="y")])
    msgs = s.get("a")
    assert msgs == []  # 预算不足时不保留超限消息


def test_trim_no_cut_when_within_limit():
    s = ConversationStore(max_turns=10)
    s.save("a", _mk())
    assert len(s.get("a")) == 2


def test_trim_keeps_tool_messages_with_turn():
    # tool 消息跟随其 user 轮次，不应被误剪
    s = ConversationStore(max_turns=1)
    s.save("a", [HumanMessage(content="q1"), ToolMessage(content="ok", tool_call_id="t1"), AIMessage(content="a1")])
    s.save("a", [HumanMessage(content="q2"), AIMessage(content="a2")])
    msgs = s.get("a")
    assert [type(m).__name__ for m in msgs] == ["HumanMessage", "AIMessage"]


# ── LRU 淘汰 ──────────────────────────────────────────────────────────────────
def test_lru_evicts_oldest():
    s = ConversationStore(max_sessions=2)
    s.save("a", _mk())
    s.save("b", _mk())
    s.get("a")  # 刷新 a 的访问时间 → b 成为最旧
    s.save("c", _mk())
    assert s.get("b") == []  # b 被淘汰
    assert len(s.get("a")) == 2 and len(s.get("c")) == 2


# ── TTL 过期 ──────────────────────────────────────────────────────────────────
def test_ttl_expiry():
    s = ConversationStore(ttl=1)
    s.save("a", _mk())
    time.sleep(1.1)
    assert s.get("a") == []


def test_ttl_refresh_on_access():
    s = ConversationStore(ttl=60)
    s.save("a", _mk())
    # 立即再 get，不应过期
    assert len(s.get("a")) == 2


# ── 清理与统计 ────────────────────────────────────────────────────────────────
def test_clear_and_clear_all():
    s = ConversationStore()
    s.save("a", _mk())
    s.save("b", _mk())
    s.clear("a")
    assert s.get("a") == [] and len(s.get("b")) == 2
    s.clear_all()
    assert s.get("b") == []


def test_stats():
    s = ConversationStore(max_turns=5, max_sessions=3)
    s.save("a", _mk())
    st = s.stats()
    assert st == {"active_sessions": 1, "max_sessions": 3, "max_turns": 5}
