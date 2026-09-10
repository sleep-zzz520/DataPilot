"""LLM/DB 工厂（core/factories.py）单测：构造实例、密钥解析、缓存失效。"""
import pytest
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

from app.core.factories import (
    build_llm_from, build_llm, build_engine, invalidate,
    PROVIDER_DEFAULT_BASE_URL, _resolve_secret, mysql_engine_options,
)
from app.core.timeouts import (
    DB_CONNECT_TIMEOUT_SECONDS,
    DB_POOL_TIMEOUT_SECONDS,
    DB_READ_TIMEOUT_SECONDS,
    DB_WRITE_TIMEOUT_SECONDS,
    LLM_MAX_RETRIES,
    LLM_REQUEST_TIMEOUT_SECONDS,
)
from app.meta import crypto


# ── provider 分支（仅对象构造，不联网）───────────────────────────────────────
def test_openai_provider_returns_chat_openai():
    llm = build_llm_from("openai", "gpt-4o", "sk-1", "https://api.openai.com/v1", temperature=0.5)
    assert isinstance(llm, ChatOpenAI)
    assert llm.temperature == 0.5
    assert llm.request_timeout == LLM_REQUEST_TIMEOUT_SECONDS
    assert llm.max_retries == LLM_MAX_RETRIES


def test_qwen_uses_openai_compatible():
    llm = build_llm_from("qwen", "qwen-max", "sk-2", None)
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "qwen-max"


def test_anthropic_provider():
    llm = build_llm_from("anthropic", "claude-3-5-sonnet", "sk-3", None)
    assert isinstance(llm, ChatAnthropic)
    assert llm.model == "claude-3-5-sonnet"
    assert llm.default_request_timeout == LLM_REQUEST_TIMEOUT_SECONDS
    assert llm.max_retries == LLM_MAX_RETRIES


def test_default_provider_is_openai():
    assert isinstance(build_llm_from(None, "gpt-4o-mini", "sk-1", None), ChatOpenAI)


def test_provider_default_base_urls():
    assert "openai" in PROVIDER_DEFAULT_BASE_URL
    assert "anthropic" in PROVIDER_DEFAULT_BASE_URL
    assert "qwen" in PROVIDER_DEFAULT_BASE_URL


# ── 密钥解析 ──────────────────────────────────────────────────────────────────
def test_resolve_secret_encrypted_wins(isolated_storage):
    cfg = {"api_key_enc": crypto.encrypt("secret"), "api_key": "plain"}
    assert _resolve_secret(cfg, "api_key_enc", "api_key") == "secret"


def test_resolve_secret_plain_fallback(isolated_storage):
    assert _resolve_secret({"api_key": "plain"}, "api_key_enc", "api_key") == "plain"
    assert _resolve_secret({}, "api_key_enc", "api_key") == ""


# ── 缓存与失效 ────────────────────────────────────────────────────────────────
def test_build_llm_caches(isolated_storage):
    cfg = {"id": 1, "provider": "openai", "model_name": "gpt-4o", "base_url": None,
           "temperature": 0, "api_key_enc": crypto.encrypt("sk-1")}
    a = build_llm(cfg)
    b = build_llm(cfg)
    assert a is b
    invalidate(llm_cfg=cfg)
    c = build_llm(cfg)
    assert c is not a


def test_build_engine_uri_construction(isolated_storage):
    # 只验证缓存键与构造行为，不真正连接（create_engine 是惰性的）
    cfg = {"id": 1, "db_type": "mysql", "username": "root", "host": "127.0.0.1",
           "port": 3306, "charset": "utf8mb4", "password_enc": crypto.encrypt("pw")}
    e1 = build_engine(cfg)
    e2 = build_engine(cfg)
    assert e1 is e2
    invalidate(db_cfg=cfg)
    assert build_engine(cfg) is not e1


def test_mysql_engine_options_bound_connection_and_pool_waits():
    options = mysql_engine_options()
    assert options["pool_timeout"] == DB_POOL_TIMEOUT_SECONDS
    assert options["max_overflow"] == 0
    assert options["connect_args"] == {
        "connect_timeout": DB_CONNECT_TIMEOUT_SECONDS,
        "read_timeout": DB_READ_TIMEOUT_SECONDS,
        "write_timeout": DB_WRITE_TIMEOUT_SECONDS,
    }
