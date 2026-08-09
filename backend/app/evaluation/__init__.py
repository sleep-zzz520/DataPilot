"""离线分析评测：对计划、SQL、质量校验和证据链做确定性打分。"""

from app.evaluation.evaluator import (
    DEFAULT_CASES_PATH,
    evaluate_case,
    evaluate_dataset,
    load_cases,
)

__all__ = [
    "DEFAULT_CASES_PATH",
    "evaluate_case",
    "evaluate_dataset",
    "load_cases",
]
