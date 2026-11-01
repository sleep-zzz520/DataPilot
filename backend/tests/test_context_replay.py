"""确定性回放oracle与报告结构回归，不需要真实模型或本机基线文件。"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.evaluation.context_replay import protocol_errors, replay_scenarios, run_replay
from app.memory import store


def test_protocol_oracle_detects_missing_or_duplicate_parallel_responses():
    request = AIMessage(content="", tool_calls=[{"id": "a", "name": "q", "args": {}},
                                               {"id": "b", "name": "q", "args": {}}])
    a = ToolMessage(content="a", tool_call_id="a")
    b = ToolMessage(content="b", tool_call_id="b")
    assert protocol_errors([request, b, a]) == []
    assert protocol_errors([request, a])
    assert protocol_errors([request, a, a, b])
    assert protocol_errors([request, HumanMessage(content="new")])
    assert protocol_errors([a])


def test_replay_uses_same_inputs_and_current_cost_oracle(tmp_path):
    # 使用真实当前模块作自比较，验证回放自身不存在随机输入/两侧计量偏差。
    report = run_replay(store.__file__, budgets=(-1, 0, 128, 2048))
    assert report["scenario_count"] >= 5 and report["case_count"] >= 20
    assert report["llm_calls"] == 0
    assert report["summary"]["before"] == report["summary"]["after"]
    for case in report["cases"]:
        assert case["before"] == case["after"]
        assert case["after"]["protocol_valid"]
        assert case["after"]["within_estimated_budget"]
        assert case["after"]["input_unchanged"]
    repeated = run_replay(store.__file__, budgets=(-1, 0, 128, 2048))
    assert report == repeated


def test_replay_scenarios_cover_empty_content_large_calls_and_invalid_history():
    scenarios = {scenario.name: scenario for scenario in replay_scenarios()}
    large = scenarios["large_empty_content_tool_args"].messages[1]
    assert large.content == "" and store.estimate_message_tokens(large) > 1000
    assert not protocol_errors(scenarios["parallel_tools"].messages)
    assert protocol_errors(scenarios["orphan_tool_input"].messages)
    assert len(scenarios["mixed_language_long_history"].messages) >= 100
