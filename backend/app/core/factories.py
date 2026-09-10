# app/core/factories.py —— 兼容明文/加密两种 store；配置用键访问，不再用属性。
from sqlalchemy import create_engine
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from app.meta import crypto
from app.core.timeouts import (
    DB_CONNECT_TIMEOUT_SECONDS,
    DB_POOL_TIMEOUT_SECONDS,
    DB_READ_TIMEOUT_SECONDS,
    DB_WRITE_TIMEOUT_SECONDS,
    LLM_MAX_RETRIES,
    LLM_REQUEST_TIMEOUT_SECONDS,
)

def _llm_fp(o): return (o.get("id"), o.get("provider"), o.get("base_url"), o.get("model_name"), o.get("temperature", 0))
def _db_fp(o):  return (o.get("id"), o.get("host"), o.get("port"), o.get("username"), o.get("default_schema"))

_llm_cache, _db_cache = {}, {}

def _resolve_secret(o, enc_key, plain_key):
    enc = o.get(enc_key)
    return crypto.decrypt(enc) if enc else (o.get(plain_key, "") or "")   # 加密store解密；明文store(当前)直取

# 各提供商默认接口地址（前端表单联动；后端做兜底）
PROVIDER_DEFAULT_BASE_URL = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}


def mysql_engine_options():
    """所有 MySQL Engine（业务执行和连通性校验）共用的资源上限。"""
    return {
        "pool_pre_ping": True,
        "pool_recycle": 3600,
        "pool_size": 5,
        "max_overflow": 0,
        "pool_timeout": DB_POOL_TIMEOUT_SECONDS,
        "connect_args": {
            "connect_timeout": DB_CONNECT_TIMEOUT_SECONDS,
            "read_timeout": DB_READ_TIMEOUT_SECONDS,
            "write_timeout": DB_WRITE_TIMEOUT_SECONDS,
        },
    }

def build_llm_from(provider, model_name, api_key, base_url, temperature=0, max_tokens=None):
    """按提供商构建 LLM 实例。
    - openai / qwen：OpenAI 兼容接口（ChatOpenAI）
    - anthropic：官方 Anthropic SDK（ChatAnthropic）
    """
    provider = (provider or "openai").lower()
    url = (base_url or "").strip() or None
    if provider == "anthropic":
        return ChatAnthropic(
            model=model_name,
            api_key=api_key,
            base_url=url,
            temperature=float(temperature or 0),
            max_tokens=max_tokens or 4096,
            timeout=LLM_REQUEST_TIMEOUT_SECONDS,
            max_retries=LLM_MAX_RETRIES,
        )
    # openai / qwen 统一走 OpenAI 兼容协议
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=url,
        temperature=float(temperature or 0),
        max_tokens=max_tokens or None,
        timeout=LLM_REQUEST_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )

def build_llm(o):
    fp = _llm_fp(o)
    if fp in _llm_cache: return _llm_cache[fp]
    c = build_llm_from(
        provider=o.get("provider"),
        model_name=o.get("model_name"),
        api_key=_resolve_secret(o, "api_key_enc", "api_key"),
        base_url=o.get("base_url"),
        temperature=o.get("temperature", 0),
        max_tokens=o.get("max_tokens"),
    )
    _llm_cache[fp] = c
    return c

def build_engine(o):
    fp = _db_fp(o)
    if fp in _db_cache: return _db_cache[fp]
    pwd = _resolve_secret(o, "password_enc", "password")
    uri = (f"{o.get('db_type')}+pymysql://{o.get('username')}:{pwd}"
           f"@{o.get('host')}:{o.get('port')}/?charset={o.get('charset') or 'utf8mb4'}")  # 不带库名，支持多库
    e = create_engine(uri, **mysql_engine_options())
    _db_cache[fp] = e
    return e

def invalidate(llm_cfg=None, db_cfg=None):
    if llm_cfg is not None: _llm_cache.pop(_llm_fp(llm_cfg), None)
    if db_cfg is not None:  _db_cache.pop(_db_fp(db_cfg), None)
