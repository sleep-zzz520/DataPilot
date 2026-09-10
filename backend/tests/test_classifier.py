"""错误分类器（errors/classifier.py）全分支单测。"""

from app.errors.classifier import (
    classify_llm_error,
    classify_db_error,
    classify_any,
)


class _Resp:
    def __init__(self, status_code=None, text=None):
        self.status_code = status_code
        self.text = text


class _Err(Exception):
    """可模拟各种异常形状（openai / dashscope / pymysql 风格）。"""

    def __init__(self, msg="", status_code=None, body=None, code=None, orig=None):
        super().__init__(msg)
        self.response = _Resp(status_code, body) if status_code is not None else None
        self.status_code = status_code
        self.body = body
        self.code = code
        self.orig = orig


def _code_of(res: dict) -> str:
    return res["code"]


# ── LLM 错误 ──────────────────────────────────────────────────────────────────
def test_llm_quota_by_status():
    assert _code_of(classify_llm_error(_Err(status_code=402))) == "LLM_QUOTA"


def test_llm_quota_by_body():
    for body in ("insufficient_quota", "Arrearage", "Account balance is not enough", "欠费", "余额不足"):
        assert _code_of(classify_llm_error(_Err(body=body))) == "LLM_QUOTA", body


def test_llm_rate_limit():
    assert _code_of(classify_llm_error(_Err(status_code=429))) == "LLM_RATE_LIMIT"
    assert _code_of(classify_llm_error(_Err(body="Throttling"))) == "LLM_RATE_LIMIT"


def test_llm_auth():
    assert _code_of(classify_llm_error(_Err(status_code=401))) == "LLM_AUTH"
    assert _code_of(classify_llm_error(_Err(body="InvalidApiKey"))) == "LLM_AUTH"


def test_llm_model_not_found():
    r = classify_llm_error(_Err(status_code=404, body="ModelNotFound"))
    assert r["code"] == "LLM_MODEL"
    assert "ModelNotFound" in r["message"]
    assert r["severity"] == "block"


def test_llm_context_too_long():
    assert _code_of(classify_llm_error(_Err(body="maximum context length"))) == "LLM_CONTEXT"


def test_llm_network():
    assert _code_of(classify_llm_error(_Err("Connection refused"))) == "LLM_NETWORK"


def test_llm_timeout_has_dedicated_code():
    assert _code_of(classify_llm_error(_Err("ReadTimeout: timed out"))) == "LLM_TIMEOUT"


def test_llm_unknown():
    r = classify_llm_error(_Err("some weird error"))
    assert r["code"] == "LLM_UNKNOWN" and r["severity"] == "warn"
    assert "some weird error" in r["suggestion"]


# ── DB 错误 ───────────────────────────────────────────────────────────────────
def test_db_connect():
    assert _code_of(classify_db_error(_Err(orig=Exception(2003, "Can't connect")))) == "DB_CONNECT"


def test_db_server_timeout_has_dedicated_code():
    error = _Err(orig=Exception(3024, "maximum statement execution time exceeded"))
    assert _code_of(classify_db_error(error)) == "DB_TIMEOUT"


def test_db_auth():
    assert _code_of(classify_db_error(_Err(orig=Exception(1045, "Access denied")))) == "DB_AUTH"


def test_db_no_db():
    assert _code_of(classify_db_error(_Err(orig=Exception(1049, "Unknown database")))) == "DB_NO_DB"


def test_db_perm():
    for errno in (1044, 1142):
        assert _code_of(classify_db_error(_Err(orig=Exception(errno, "denied")))) == "DB_PERM", errno


def test_db_sql_syntax_retry():
    r = classify_db_error(_Err(orig=Exception(1064, "syntax error")))
    assert r["code"] == "DB_SQL_SYNTAX" and r["severity"] == "retry"


def test_db_lock_retry():
    for errno in (1205, 1213):
        assert _code_of(classify_db_error(_Err(orig=Exception(errno, "lock")))) == "DB_LOCK", errno


def test_db_unknown():
    r = classify_db_error(_Err(orig=Exception(9999, "weird")))
    assert r["code"] == "DB_UNKNOWN"


# ── classify_any：db / llm 路由 ───────────────────────────────────────────────
def test_classify_any_routes_mysql():
    assert _code_of(classify_any(_Err(orig=Exception(1045, "Access denied")))) == "DB_AUTH"
    assert _code_of(classify_any(_Err("pymysql.err.OperationalError: deadlock"))) == "DB_UNKNOWN"


def test_classify_any_routes_llm():
    assert _code_of(classify_any(_Err(status_code=429))) == "LLM_RATE_LIMIT"
