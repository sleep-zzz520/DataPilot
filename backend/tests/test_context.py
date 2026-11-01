"""上下文压缩（memory/store.py 的 token 估算与预算裁剪）单测。"""
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

from app.memory.store import (estimate_tokens, budget_history, estimate_message_tokens,
                              estimate_messages_tokens, estimate_tool_tokens)


# ── token 估算 ────────────────────────────────────────────────────────────────
def test_estimate_tokens_basic():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("a") >= 1


def test_estimate_tokens_cjk_heavier():
    # 中文按 ~1.5 token/字，应显著高于同长度 ASCII
    assert estimate_tokens("你" * 100) > estimate_tokens("a" * 100)


def test_estimate_tokens_monotonic():
    assert estimate_tokens("你好世界") > estimate_tokens("你好")
    assert estimate_tokens("hello world") > estimate_tokens("hello")


# ── 预算裁剪 ──────────────────────────────────────────────────────────────────
def _msgs(*texts):
    return [HumanMessage(content=t) for t in texts]


def test_budget_history_keeps_recent():
    msgs = _msgs("短" * 10, "短" * 10, "短" * 10, "短" * 10)
    out = budget_history(msgs, budget=estimate_message_tokens(msgs[-1]) + 5)  # 只能装下 1 条多一点
    assert len(out) <= 2
    assert out[-1].content == "短" * 10  # 保留最近的


def test_budget_history_keeps_order():
    msgs = _msgs("长" * 2000, "短")
    out = budget_history(msgs, budget=100)
    assert [m.content for m in out] == ["短"]  # 长消息超预算被裁，保留最近


def test_budget_history_oversized_message_returns_empty():
    msgs = _msgs("很长" * 10000)
    out = budget_history(msgs, budget=10)
    assert out == []  # 必须遵守预算，当前用户输入由共享组装器保护


def test_budget_history_does_not_mutate_input():
    msgs = _msgs("短" * 10, "短" * 10, "短" * 10, "短" * 10)
    before = list(msgs)
    budget_history(msgs, budget=1)
    assert msgs == before


def test_budget_history_zero_or_empty():
    assert budget_history([], 100) == []
    msgs = _msgs("a")
    assert budget_history(msgs, 0) == []
    assert budget_history(msgs, -100) == []


def test_budget_history_budget_includes_system_prompt():
    # 模拟 chat 接口用法：预算扣除 system prompt 后再裁历史
    system = SystemMessage(content="你" * 100)          # ≈150 token
    history = _msgs("你" * 100, "你" * 100, "你" * 100)  # 各 ≈150 token，共 450
    budget = 400
    out = budget_history(history, budget - estimate_tokens(system.content))
    assert len(out) < 3  # 预算不足 → 头部被裁
    assert out[-1].content == "你" * 100  # 保留最近
    used = estimate_tokens(system.content) + sum(estimate_tokens(m.content) for m in out)
    assert used <= budget + estimate_tokens("你" * 100)  # 含最后一条可略超


def test_budget_history_works_with_all_message_types():
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content="q1"),
        ToolMessage(content="tool out", tool_call_id="t1"),
        AIMessage(content="a1"),
        HumanMessage(content="q2"),
    ]
    out = budget_history(msgs, budget=100)
    assert out[-1].content == "q2"


def _tool_chain():
    return [AIMessage(content="", tool_calls=[{"id": "t1", "name": "query", "args": {"sql": "select 1"}},
                                              {"id": "t2", "name": "query", "args": {"sql": "select 2"}}]),
            ToolMessage(content="first", tool_call_id="t1"),
            ToolMessage(content="second", tool_call_id="t2")]


def test_tool_calls_count_even_when_content_empty():
    call = _tool_chain()[0]
    assert estimate_message_tokens(call) > estimate_message_tokens(AIMessage(content=""))
    large = AIMessage(content="", tool_calls=[{"id": "t", "name": "query", "args": {"sql": "x" * 4000}}])
    assert estimate_message_tokens(large) > 1000
    assert estimate_tokens([{"type": "text", "text": "你好"}]) > 0


def test_parallel_tool_chain_is_atomic_for_every_budget():
    chain = _tool_chain()
    messages = [HumanMessage(content="old")] + chain + [AIMessage(content="done")]
    for budget in range(300):
        out = budget_history(messages, budget)
        assert estimate_messages_tokens(out) <= budget
        retained = [message in out for message in chain]
        assert len(set(retained)) == 1
    assert budget_history(chain, estimate_messages_tokens(chain)) == chain


def test_orphan_and_incomplete_tool_calls_removed():
    chain = _tool_chain()
    messages = [ToolMessage(content="orphan", tool_call_id="missing"), *chain[:2], HumanMessage(content="new")]
    assert budget_history(messages, 10000) == [messages[-1]]


def test_assemble_context_accounts_for_all_required_input():
    from app.memory.context import assemble_context, ContextBudgetExceeded
    import pytest
    tool = {"type": "function", "function": {"name": "query", "description": "查询" * 30,
                                           "parameters": {"type": "object", "properties": {}}}}
    history = [HumanMessage(content="old" * 50)] * 30
    result = assemble_context("rules", history, "current", memory_context="memory\n" * 20,
                              task_context="current task", tools=[tool], max_tokens=500, output_reserve=80)
    assert result.estimated_tokens <= 500
    assert result.estimated_tokens == estimate_messages_tokens(result.messages) + estimate_tool_tokens([tool]) + 80
    assert result.messages[-1].content == "current"
    assert "current task" in result.system_prompt
    assert result.dropped_messages > 0
    with pytest.raises(ContextBudgetExceeded):
        assemble_context("rules", history, "巨大" * 100, max_tokens=100, output_reserve=80)


def test_bound_context_preserves_active_tool_turn_in_order():
    from app.memory.context import bound_context_messages, ContextBudgetExceeded
    import pytest
    active = [HumanMessage(content="current"), *_tool_chain()]
    result = bound_context_messages("rules", [HumanMessage(content="old" * 100)] + active,
                                    max_tokens=400, output_reserve=80)
    assert result.messages[-len(active):] == active
    assert result.estimated_tokens <= 400
    with pytest.raises(ContextBudgetExceeded):
        bound_context_messages("rules", active, max_tokens=50)
