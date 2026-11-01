"""持久化（persistence/__init__.py）单测：序列化纯函数 + SQLite 功能（临时库）。"""
import pytest
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage

from app.persistence import (
    _ser, _deser, _fingerprint, _auto_title,
    create_user, get_user_by_username, list_users, update_user_role,
    update_password, delete_user, claim_orphan_data,
    persist_messages, load_session, list_sessions, delete_session,
    rename_session, get_session_info, save_upload, get_upload,
    save_trace, load_traces,
)
from app.auth import hash_password


# ── 序列化 / 反序列化 ─────────────────────────────────────────────────────────
def test_ser_human():
    role, content, extra = _ser(HumanMessage(content="hi", id="m1"))
    assert (role, content) == ("user", "hi")
    assert "msg_id" in extra


def test_ser_deser_roundtrip_all_types():
    msgs = [
        HumanMessage(content="q", id="1"),
        AIMessage(content="a", tool_calls=[{"name": "query_mysql", "args": {"sql": "S"}, "id": "c1"}], id="2"),
        ToolMessage(content="ok", tool_call_id="c1", name="query_mysql", id="3"),
        SystemMessage(content="sys", id="4"),
    ]
    for m in msgs:
        role, content, extra = _ser(m)
        back = _deser(role, content, extra)
        assert type(back) is type(m)
        assert back.content == m.content
        if isinstance(m, ToolMessage):
            assert back.tool_call_id == m.tool_call_id
        if isinstance(m, AIMessage) and m.tool_calls:
            assert back.tool_calls == m.tool_calls


def test_deser_unknown_role_falls_back():
    m = _deser("weird", "x", None)
    assert m.content == "[weird] x"


# ── 工具函数 ──────────────────────────────────────────────────────────────────
def test_fingerprint_stable_and_distinct():
    assert _fingerprint("user", "a", None) == _fingerprint("user", "a", None)
    assert _fingerprint("user", "a", None) != _fingerprint("user", "b", None)
    assert _fingerprint("user", "a", None) != _fingerprint("assistant", "a", None)


def test_auto_title():
    msgs = [AIMessage(content="先回复"), HumanMessage(content="  帮我分析订单数据  ")]
    assert _auto_title(msgs) == "帮我分析订单数据"
    assert _auto_title([HumanMessage(content="x" * 50)]) == "x" * 30 + "..."
    assert _auto_title([]) == "新对话"
    assert _auto_title([AIMessage(content="no user")]) == "新对话"


# ── 用户（临时 SQLite）────────────────────────────────────────────────────────
def test_first_user_is_admin(isolated_storage):
    u = create_user("boss", hash_password("p"))
    assert u["role"] == "admin"
    assert create_user("worker", hash_password("p"))["role"] == "user"


def test_create_user_duplicate_returns_none(isolated_storage):
    create_user("alice", "h1")
    assert create_user("alice", "h2") is None


def test_get_user_and_list(isolated_storage):
    create_user("alice", "h")
    row = get_user_by_username("alice")
    assert row["password_hash"] == "h" and row["role"] == "admin"
    users = list_users()
    assert users[0]["username"] == "alice"
    assert "password_hash" not in users[0]


def test_update_user_role(isolated_storage):
    u = create_user("alice", "h")
    assert update_user_role(u["id"], "user") is True
    assert get_user_by_username("alice")["role"] == "user"
    assert update_user_role(u["id"], "superadmin") is False  # 非法角色
    assert update_user_role(999, "user") is False           # 不存在


def test_update_password(isolated_storage):
    u = create_user("alice", "old")
    assert update_password(u["id"], "new") is True
    assert get_user_by_username("alice")["password_hash"] == "new"


def test_claim_orphan_data(isolated_storage):
    u = create_user("alice", "h")
    persist_messages("orphan-session", [HumanMessage(content="无人认领")], user_id=None)
    claim_orphan_data(u["id"])
    assert get_session_info("orphan-session", u["id"]) is not None


# ── Agent 轨迹（traces 表）───────────────────────────────────────────────────
def test_save_and_load_traces_roundtrip(isolated_storage):
    u = create_user("alice", "h")
    sid = "trace-session"
    persist_messages(sid, [HumanMessage(content="q1"), AIMessage(content="a1")], user_id=u["id"])
    assert save_trace(sid, [{"seq": 1, "tool": "query_mysql"}], u["id"]) is True
    # 第二轮：turn_seq 应自动对齐到 1（第一轮为 0）
    persist_messages(sid, [
        HumanMessage(content="q1"), AIMessage(content="a1"),
        HumanMessage(content="q2"), AIMessage(content="a2"),
    ], user_id=u["id"])
    assert save_trace(sid, [{"seq": 1, "tool": "make_chart"}], u["id"]) is True
    traces = load_traces(sid, u["id"])
    assert [t[0]["tool"] for t in traces] == ["query_mysql", "make_chart"]


def test_traces_user_isolated(isolated_storage):
    u1 = create_user("alice", "h")
    u2 = create_user("bob", "h")
    persist_messages("s1", [HumanMessage(content="q"), AIMessage(content="a")], user_id=u1["id"])
    assert save_trace("s1", [{"tool": "x"}], u1["id"]) is True
    # 其他用户不可读，也不可写
    assert load_traces("s1", u2["id"]) == []
    assert save_trace("s1", [{"tool": "x"}], u2["id"]) is False


def test_traces_cascade_deleted_with_session(isolated_storage):
    u = create_user("alice", "h")
    persist_messages("s1", [HumanMessage(content="q"), AIMessage(content="a")], user_id=u["id"])
    save_trace("s1", [{"tool": "x"}], u["id"])
    assert delete_session("s1", u["id"]) is True
    assert load_traces("s1", u["id"]) == []


def test_claim_orphan_then_load_session_visible(isolated_storage):
    """回归用例：存量无主会话，认领后（登录时触发）应能被 load_session 恢复出来。"""
    u = create_user("alice", "h")
    persist_messages("legacy", [HumanMessage(content="旧会话", id="m1")], user_id=None)
    # 认领前：按当前用户查不到
    assert load_session("legacy", user_id=u["id"]) == []
    claim_orphan_data(u["id"])
    loaded = load_session("legacy", user_id=u["id"])
    assert [m.content for m in loaded] == ["旧会话"]


def test_delete_user_returns_upload_paths(isolated_storage):
    u = create_user("alice", "h")
    save_upload("f1", "a.csv", "/tmp/uploads/a.csv", ["c"], "[]", u["id"])
    paths = delete_user(u["id"])
    assert paths == ["/tmp/uploads/a.csv"]
    assert get_user_by_username("alice") is None
    assert get_upload("f1", u["id"]) is None  # 上传记录已级联删除


# ── 会话消息（临时 SQLite）────────────────────────────────────────────────────
def test_persist_and_load_session(isolated_storage):
    msgs = [HumanMessage(content="你好", id="m1"), AIMessage(content="你好！", id="m2")]
    persist_messages("s1", msgs, user_id=1)
    loaded = load_session("s1", user_id=1)
    assert [m.content for m in loaded] == ["你好", "你好！"]
    assert loaded[0].id == "m1"


def test_persist_is_idempotent(isolated_storage):
    msgs = [HumanMessage(content="q", id="m1"), AIMessage(content="a", id="m2")]
    persist_messages("s1", msgs, user_id=1)
    persist_messages("s1", msgs, user_id=1)  # 再存一遍：全部去重
    loaded = load_session("s1", user_id=1)
    assert len(loaded) == 2


def test_persist_appends_only_new(isolated_storage):
    round1 = [HumanMessage(content="q1", id="m1"), AIMessage(content="a1", id="m2")]
    round2 = round1 + [HumanMessage(content="q2", id="m3"), AIMessage(content="a2", id="m4")]
    persist_messages("s1", round1, user_id=1)
    persist_messages("s1", round2, user_id=1)
    assert [m.content for m in load_session("s1", user_id=1)] == ["q1", "a1", "q2", "a2"]


def test_auto_title_from_first_user_message(isolated_storage):
    persist_messages("s1", [HumanMessage(content="帮我统计上个月的销售情况"), AIMessage(content="好")], user_id=1)
    info = get_session_info("s1")
    assert "帮我统计上个月" in info["title"]


def test_list_sessions_ordered_and_scoped(isolated_storage):
    persist_messages("s1", [HumanMessage(content="q", id="1")], user_id=1)
    persist_messages("s2", [HumanMessage(content="q", id="2")], user_id=1)
    persist_messages("s3", [HumanMessage(content="q", id="3")], user_id=2)
    rows = list_sessions(user_id=1)
    assert {r["id"] for r in rows} == {"s1", "s2"}
    assert all(r["msg_count"] >= 1 for r in rows)
    assert list_sessions(limit=1, user_id=1)[0]["msg_count"] == 1


def test_delete_and_rename_session(isolated_storage):
    persist_messages("s1", [HumanMessage(content="q", id="1")], user_id=1)
    assert rename_session("s1", "新标题", user_id=1) is True
    assert get_session_info("s1")["title"] == "新标题"
    assert rename_session("s1", "x", user_id=2) is False  # 非本人
    assert delete_session("s1", user_id=1) is True
    assert delete_session("s1", user_id=1) is False
    assert load_session("s1", user_id=1) == []


def test_session_scoped_by_user(isolated_storage):
    persist_messages("s1", [HumanMessage(content="q", id="1")], user_id=1)
    assert load_session("s1", user_id=2) == []  # 他人不可见


def test_pending_guard_hides_raw_candidates_and_visuals_after_restart(isolated_storage):
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
    from app.persistence import persist_messages, get_display_messages, load_session
    raw = [HumanMessage(content="统计订单", id="guard-user"),
           ToolMessage(content='<!--CHART:{"type":"bar"}--><!--TABLE:{"rows":[[999]]}-->',
                       tool_call_id="c", name="make_chart", id="guard-tool"),
           AIMessage(content="候选结论999", id="guard-answer")]
    persist_messages("guard-pending", raw, 7, pending_guard=True)
    # Every read opens a new database connection, including the first after restart.
    displayed = get_display_messages("guard-pending")
    assert displayed[-1]["role"] == "assistant"
    assert "999" not in displayed[-1]["text"]
    assert not displayed[-1].get("visuals")
    assert not displayed[-1].get("tables")
    assert load_session("guard-pending", 7)[-1].content == "候选结论999"


def test_guarded_response_overrides_one_owned_turn_and_keeps_audit(isolated_storage):
    import pytest
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
    from app.persistence import (persist_messages, persist_guarded_response,
                                 get_display_messages, load_session)
    persist_messages("guard-final", [
        HumanMessage(content="第一轮", id="turn-one"),
        ToolMessage(content='<!--CHART:{"type":"bar"}-->', tool_call_id="a", id="tool-a"),
        AIMessage(content="候选999", id="answer-a"),
        HumanMessage(content="第二轮", id="turn-two"),
        AIMessage(content="第二轮候选", id="answer-b"),
    ], 7, pending_guard=True)
    with pytest.raises(ValueError):
        persist_guarded_response("guard-final", 8, "turn-one", "越权覆盖")
    with pytest.raises(ValueError):
        persist_guarded_response("guard-final", 7, "missing-id", "不存在的轮次")
    persist_guarded_response("guard-final", 7, "turn-one", "本轮验证不通过")
    persist_guarded_response("guard-final", 7, "turn-two", "真实结果120", visuals=[{"chart": {"type": "line"}}],
                             tables=[{"rows": [[120]]}], sql="SELECT 120")
    replies = [m for m in get_display_messages("guard-final") if m["role"] == "assistant"]
    assert replies[0]["text"] == "本轮验证不通过"
    assert not replies[0].get("visuals")
    assert replies[1]["text"] == "真实结果120"
    assert replies[1]["visuals"] == [{"chart": {"type": "line"}}]
    assert replies[1]["tables"] == [{"rows": [[120]]}]
    assert replies[1]["sql"] == "SELECT 120"
    assert any(m.content == "候选999" for m in load_session("guard-final", 7))
