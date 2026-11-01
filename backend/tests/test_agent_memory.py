"""Agent 长期记忆（agent_memory.py）单测：存取/提取节流/摘要/检索/注入。"""
import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.memory.agent_memory import (
    extract_and_store_memories, get_user_memories,
    summary_due, generate_summary, get_summary, get_summary_info,
    retrieve_relevant, retrieve_relevant_analysis, save_analysis_record,
    delete_analysis_records, _keywords, build_memory_context,
    _has_memory_signal, _extract_log,
)
from app.persistence import persist_messages


class FakeLLM(BaseChatModel):
    """按调用顺序返回预设回复。"""
    responses: list = []
    calls: list = []
    _i: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(list(messages))
        r = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=r))])


@pytest.fixture(autouse=True)
def _clean():
    _extract_log.clear()
    yield
    _extract_log.clear()


# ── 信号词与节流 ──────────────────────────────────────────────────────────────
def test_memory_signal_words():
    assert _has_memory_signal("我常用 share-order 库")
    assert _has_memory_signal("以后都用柱状图")
    assert _has_memory_signal("默认按月汇总")
    assert not _has_memory_signal("帮我查一下订单")


def test_extract_rate_limited_by_session(isolated_storage):
    llm = FakeLLM(responses=['[{"key": "常用库", "value": "share-order"}]'])
    # 第一次提取成功
    assert extract_and_store_memories(llm, 1, "alice", "s1", "我常用 share-order 库", "好的") == 1
    # 同一会话 10 分钟内再次 → 被节流（不调 LLM，返回 0）
    assert extract_and_store_memories(llm, 1, "alice", "s1", "我常用 share-payment 库", "好的") == 0
    # 其他会话不受影响
    assert extract_and_store_memories(llm, 1, "alice", "s2", "我常用 share-order 库", "好的") == 1


def test_extract_skips_without_signal(isolated_storage):
    llm = FakeLLM(responses=['[{"key": "k", "value": "v"}]'])
    assert extract_and_store_memories(llm, 1, "alice", "s1", "帮我查订单", "好的") == 0  # 无信号词不提取


def test_extract_bad_json_ignored(isolated_storage):
    llm = FakeLLM(responses=["这不是 JSON"])
    assert extract_and_store_memories(llm, 1, "alice", "s1", "我常用 share-order 库", "好的") == 0


def test_memory_maintenance_requests_include_user_message(isolated_storage):
    """兼容要求至少一条 user 消息的 OpenAI 兼容模型（如 GLM）。"""
    llm = FakeLLM(responses=['[{"key": "图表偏好", "value": "折线图"}]', "订单趋势摘要"])

    assert extract_and_store_memories(llm, 1, "alice", "s1", "我默认用折线图", "好的") == 1
    assert [type(message) for message in llm.calls[0]] == [SystemMessage, HumanMessage]

    persist_messages("s1", [HumanMessage(content="查询订单趋势")], user_id=1)
    assert generate_summary(llm, "s1", [HumanMessage(content="查询订单趋势")]) == "订单趋势摘要"
    assert [type(message) for message in llm.calls[1]] == [SystemMessage, HumanMessage]


# ── 长期记忆存取 ──────────────────────────────────────────────────────────────
def test_memories_upsert_and_get(isolated_storage):
    llm = FakeLLM(responses=['[{"key": "常用库", "value": "share-order"}]'])
    extract_and_store_memories(llm, 1, "alice", "s1", "我常用 share-order 库", "好的")
    mems = get_user_memories(1)
    assert len(mems) == 1 and mems[0]["key"] == "常用库"
    assert mems[0]["source_session"] == "s1" and mems[0]["updated_at"]
    assert get_user_memories(2) == []  # 用户隔离


def test_memory_upsert_same_key_updates(isolated_storage):
    llm = FakeLLM(responses=['[{"key": "常用库", "value": "share-order"}]',
                             '[{"key": "常用库", "value": "share-payment"}]'])
    extract_and_store_memories(llm, 1, "alice", "s1", "我常用 share-order 库", "好的")
    _extract_log.pop("s1", None)  # 绕过节流
    extract_and_store_memories(llm, 1, "alice", "s1", "我常用 share-payment 库", "好的")
    mems = get_user_memories(1)
    assert len(mems) == 1 and mems[0]["value"] == "share-payment"  # 覆盖更新


# ── 会话摘要 ──────────────────────────────────────────────────────────────────
def test_summary_due_and_generate(isolated_storage):
    llm = FakeLLM(responses=["用户分析了成绩单，偏好柱状图"])
    # 先写 5 条 user 消息
    for i in range(5):
        persist_messages("s9", [HumanMessage(content=f"问题{i}")], user_id=1)
    assert summary_due("s9") is True  # 无摘要 + 5 轮 → 到期
    s = generate_summary(llm, "s9", [HumanMessage(content="问题0"), HumanMessage(content="问题1")])
    assert s == "用户分析了成绩单，偏好柱状图"
    assert get_summary("s9") == s
    assert summary_due("s9") is False  # 刚生成 → 未到期


def test_summary_not_due_below_turns(isolated_storage):
    for i in range(2):
        persist_messages("s1", [HumanMessage(content=f"q{i}")], user_id=1)
    assert summary_due("s1") is False


def test_summary_info_contains_update_time(isolated_storage):
    llm = FakeLLM(responses=["摘要内容"])
    for i in range(5):
        persist_messages("s-summary", [HumanMessage(content=f"q{i}")], user_id=1)
    generate_summary(llm, "s-summary", [HumanMessage(content="q")])
    info = get_summary_info("s-summary")
    assert info["summary"] == "摘要内容" and info["updated_at"]


def test_generate_summary_bad_response(isolated_storage):
    llm = FakeLLM(responses=[""])
    for i in range(5):
        persist_messages("s1", [HumanMessage(content=f"q{i}")], user_id=1)
    assert generate_summary(llm, "s1", [HumanMessage(content="q")]) is None
    assert get_summary("s1") is None


# ── 检索 ──────────────────────────────────────────────────────────────────────
def test_keywords_extraction():
    # CJK 片段按连续中文整体提取（LIKE 检索用）
    assert "上个月订单数" in _keywords("上个月订单数")
    assert "share" in _keywords("看 share-order 库")  # 连字符分词后 share 可命中
    assert _keywords("帮我") == []  # 2 字停用词被过滤


def test_retrieve_relevant(isolated_storage):
    persist_messages("s1", [HumanMessage(content="帮我统计各状态订单数", id="1")], user_id=1)
    persist_messages("s1", [AIMessage(content="已完成 120 单", id="2")], user_id=1)
    persist_messages("s1", [HumanMessage(content="聊聊天气", id="3")], user_id=1)
    hits = retrieve_relevant("s1", "订单")
    assert len(hits) >= 1
    assert any("订单" in h["content"] for h in hits)
    assert all(h["session_id"] == "s1" and h["updated_at"] for h in hits)


def test_retrieve_no_keywords_returns_empty(isolated_storage):
    persist_messages("s1", [HumanMessage(content="你好")], user_id=1)
    assert retrieve_relevant("s1", "分析一下数据") == []


# ── 注入 ──────────────────────────────────────────────────────────────────────
def test_build_memory_context_combines_all(isolated_storage):
    llm = FakeLLM(responses=['[{"key": "常用库", "value": "share-order"}]',
                             "用户关注订单状态，偏好柱状图"])
    extract_and_store_memories(llm, 1, "alice", "s1", "我常用 share-order 库", "好的")
    persist_messages("s1", [HumanMessage(content="上个月订单多少", id="1")], user_id=1)
    persist_messages("s1", [AIMessage(content="上月 120 单", id="2")], user_id=1)
    for i in range(5):
        persist_messages("s1", [HumanMessage(content=f"q{i}", id=f"m{i}")], user_id=1)
    generate_summary(llm, "s1", [HumanMessage(content="q")])

    ctx = build_memory_context(1, "s1", "上个月订单多少")
    assert ctx is not None
    assert "长期记忆" in ctx and "常用库" in ctx
    assert "历史摘要" in ctx and "来源会话：s1" in ctx
    assert "相关的历史对话" in ctx and "订单" in ctx


def test_build_memory_context_none_when_empty(isolated_storage):
    assert build_memory_context(1, "no-such-session", "你好") is None


def test_analysis_record_can_be_reused_with_source_and_time(isolated_storage):
    plan = {
        "goal": "统计订单数",
        "metrics": ["订单数"],
        "dimensions": ["地区"],
        "steps": [{"id": "q", "title": "查询", "kind": "query", "depends_on": []}],
    }
    assert save_analysis_record(
        1, "s-analysis", "统计各地区订单数", plan,
        [{"source": "query_mysql", "sql": "SELECT region, COUNT(*) FROM orders", "row_count": 2}],
        "北京 120 单，上海 80 单",
    )
    hits = retrieve_relevant_analysis(1, "统计各地区订单数量")
    assert len(hits) == 1
    assert hits[0]["plan"]["goal"] == "统计订单数"
    assert hits[0]["evidence"][0]["row_count"] == 2
    ctx = build_memory_context(1, "s-current", "统计各地区订单数量")
    assert "相似历史分析" in ctx
    assert "可复用计划" in ctx and "来源会话：s-analysis" in ctx
    assert "当前数据、当前口径和当前查询结果优先" in ctx
    assert delete_analysis_records("s-analysis", 1) == 1
    assert retrieve_relevant_analysis(1, "统计各地区订单数量") == []


def test_explicit_correction_bypasses_throttle_and_tracks_version(isolated_storage):
    llm = FakeLLM(responses=['[{"key":"图表偏好","value":"折线图"}]',
                             '[{"key":"图表偏好","value":"柱状图"}]'])
    assert extract_and_store_memories(llm, 1, "a", "s", "我默认用折线图", "ok") == 1
    assert extract_and_store_memories(llm, 1, "a", "s", "更正，图表改为柱状图", "ok") == 1
    memory = get_user_memories(1)[0]
    assert memory["value"] == "柱状图" and memory["version"] == 2
    assert "改为柱状图" in memory["source_text"]


def test_scoped_preferences_and_stale_update_conflict(isolated_storage):
    from app.memory.agent_memory import _upsert_memory
    assert _upsert_memory(1, "a", "单位", "元", "s")
    assert _upsert_memory(1, "a", "单位", "万元", "s", scope="finance")
    assert get_user_memories(1, scope="finance")[0]["value"] == "万元"
    assert get_user_memories(1, scope="sales")[0]["value"] == "元"
    assert not _upsert_memory(1, "a", "单位", "错误旧值", "s", scope="finance", expected_version=0)
    assert get_user_memories(1, scope="finance")[0]["value"] == "万元"


def test_summary_uses_uncovered_sqlite_history_and_previous_summary(isolated_storage):
    llm = FakeLLM(responses=["目标保留，待办保留", "目标保留，待办完成"])
    persist_messages("s", [HumanMessage(content="长期目标及约束", id="first"),
                           HumanMessage(content="被内存裁掉的信息", id="second")], user_id=1)
    generate_summary(llm, "s", [HumanMessage(content="仅内存尾部")])
    assert "被内存裁掉的信息" in llm.calls[0][0].content
    info = get_summary_info("s")
    assert info["covered_seq"] == 1 and info["covered_message_id"] == "second"
    persist_messages("s", [HumanMessage(content="完成待办", id="third")], user_id=1)
    generate_summary(llm, "s", [])
    prompt = llm.calls[1][0].content
    assert "旧摘要：目标保留，待办保留" in prompt and "完成待办" in prompt
    assert "被内存裁掉的信息" not in prompt
    assert get_summary_info("s")["covered_message_id"] == "third"


def test_summary_large_message_retains_uncovered_offset_on_failure(isolated_storage):
    llm = FakeLLM(responses=["目标约束保留", ""])
    persist_messages("s", [HumanMessage(content="大" * 9000, id="large")], user_id=1)
    assert generate_summary(llm, "s", []) == "目标约束保留"
    info = get_summary_info("s")
    assert info["covered_seq"] == -1 and info["covered_offset"] == 4000
    assert summary_due("s")
    retry = FakeLLM(responses=["累计完成"])
    generate_summary(retry, "s", [])
    info = get_summary_info("s")
    assert info["covered_seq"] == 0 and info["covered_offset"] == 0
    assert info["covered_message_id"] == "large"


def test_memory_context_budget_and_analysis_scope(isolated_storage):
    from app.memory.agent_memory import _upsert_memory
    from app.memory.store import estimate_tokens
    _upsert_memory(1, "a", "单位", "万元", "s", scope="finance")
    assert build_memory_context(1, "s", "hello", scope="sales") is None
    result = build_memory_context(1, "s", "hello", scope="finance", max_tokens=400)
    assert result and "数据域：finance" in result and estimate_tokens(result) <= 400
    assert build_memory_context(1, "s", "hello", scope="finance", max_tokens=0) is None
    plan = {"goal": "订单统计", "metrics": ["订单数"]}
    save_analysis_record(1, "s", "订单统计", plan, [], "100单", scope="finance")
    assert retrieve_relevant_analysis(1, "订单统计", scope="sales") == []
    assert len(retrieve_relevant_analysis(1, "订单统计", scope="finance")) == 1


def test_summary_batch_cap_keeps_pending_history_due(isolated_storage):
    # 每条序列化刚好4000字符，4批后边界落在完整消息而非大消息分片。
    from app import persistence
    messages = []
    for i in range(6):
        message = HumanMessage(content="", id=f"id{i}")
        role, content, extra = persistence._ser(message)
        overhead = len(json.dumps({"role": role, "content": content, "extra": extra}, ensure_ascii=False))
        message.content = "x" * (4000 - overhead)
        messages.append(message)
    persist_messages("s", messages, user_id=1)
    llm = FakeLLM(responses=["累计摘要"])
    generate_summary(llm, "s", [])
    info = get_summary_info("s")
    assert len(llm.calls) == 4 and info["covered_seq"] == 3
    assert info["covered_offset"] == 0 and info["has_pending"]
    assert summary_due("s")  # 剩余不足5轮也必须继续补齐已知积压。
    generate_summary(llm, "s", [])
    assert not get_summary_info("s")["has_pending"]
    assert not summary_due("s")
