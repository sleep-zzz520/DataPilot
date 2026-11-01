"""Independent scoring must reject obvious failure and tolerate numeric formatting."""
from app.evaluation.architecture_benchmark import cases, dataset, score, summarize


def test_numeric_oracle_handles_chinese_and_thousands():
    case = cases(dataset())[0]
    assert score(case, "销售额合计2250，没有缺失记录。", None)
    assert score(case, "2,250.00", None)
    assert not score(case, "总额2200", None)
    assert not score(case, "无法计算，猜测2250", None)
    assert not score(case, "2250", "TimeoutError")


def test_refusal_cannot_hide_specific_unsupported_claim():
    case = {"decline": True}
    assert score(case, "缺少性别列，无法按性别分组。", None)
    assert not score(case, "缺少字段，但是销售额是2500。", None)


def test_empty_and_failed_runs_remain_in_denominator():
    rows = [dict(mode="auto", passed=False, error="Timeout", latency_s=20,
                 input_tokens=100, output_tokens=0, llm_calls=1),
            dict(mode="auto", passed=True, error=None, latency_s=2,
                 input_tokens=50, output_tokens=10, llm_calls=1)]
    result = summarize(rows)["auto"]
    assert result["runs"] == 2
    assert result["success_rate"] == .5
    assert result["errors"] == 1
