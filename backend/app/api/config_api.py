# app/api/config_api.py —— 对接加密版 store
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from app.meta import store
from app.meta import crypto
from app.audit import record, A_CONFIG_CHANGE
from app.api.auth_api import get_current_user

router = APIRouter(prefix="/api/config", tags=["config"])


def _require_admin(user: dict) -> None:
    """配置共享，仅管理员可修改。"""
    if user.get("role") != "admin":
        raise HTTPException(403, "仅管理员可修改配置")

# ---------- Request Models ----------
class LLMConfigSave(BaseModel):
    id: Optional[int] = None
    name: str
    provider: str = "openai"              # openai / anthropic / qwen
    base_url: str = ""
    model_name: str
    api_key: Optional[str] = None       # 编辑时若为空/掩码串则保留原密文
    temperature: float = 0
    max_tokens: Optional[int] = None
    is_default: bool = False

class DBConfigSave(BaseModel):
    id: Optional[int] = None
    name: str
    db_type: str
    host: str
    port: int
    username: str
    password: Optional[str] = None      # 同上
    charset: str = "utf8mb4"
    default_schema: Optional[str] = None
    is_default: bool = False

# ---------- LLM ----------
@router.get("/llm")
def list_llm(user: dict = Depends(get_current_user)):
    return store.list_llm_configs()

@router.get("/llm/{cfg_id}")
def get_llm(cfg_id: int, user: dict = Depends(get_current_user)):
    cfg = store.get_llm_config(cfg_id)
    if not cfg: raise HTTPException(404, "LLM config not found")
    return cfg

@router.post("/llm")
def save_llm(req: LLMConfigSave, user: dict = Depends(get_current_user)):
    _require_admin(user)
    payload = req.model_dump()
    # 编辑时若 api_key 是掩码串或空，保留原密文
    if payload.get("id") is not None:
        existing = store.get_llm_config(payload["id"])
        if existing and (not payload.get("api_key") or payload["api_key"].startswith(existing.get("api_key_masked", "")[:4])):
            # 用户未改 key，从原记录取回密文
            secret = store.get_llm_secret(payload["id"])
            if secret and "api_key" in secret:
                payload["api_key"] = secret["api_key"]
            else:
                payload.pop("api_key", None)
    saved = store.save_llm_config(payload)
    # 审计只记元信息，绝不落明文 api_key
    record(A_CONFIG_CHANGE, user["uid"], user.get("username"), {
        "type": "llm", "action": "update" if req.id else "create",
        "id": saved.get("id"), "name": saved.get("name"), "provider": saved.get("provider"),
        "model_name": saved.get("model_name"), "is_default": saved.get("is_default"),
    })
    return saved

@router.delete("/llm/{cfg_id}")
def delete_llm(cfg_id: int, user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not store.delete_llm_config(cfg_id):
        raise HTTPException(404, "LLM config not found")
    record(A_CONFIG_CHANGE, user["uid"], user.get("username"),
           {"type": "llm", "action": "delete", "id": cfg_id})
    return {"ok": True}

# ---------- DB ----------
@router.get("/db")
def list_db(user: dict = Depends(get_current_user)):
    return store.list_db_configs()

@router.get("/db/{cfg_id}")
def get_db(cfg_id: int, user: dict = Depends(get_current_user)):
    cfg = store.get_db_config(cfg_id)
    if not cfg: raise HTTPException(404, "DB config not found")
    return cfg

@router.post("/db")
def save_db(req: DBConfigSave, user: dict = Depends(get_current_user)):
    _require_admin(user)
    payload = req.model_dump()
    if payload.get("id") is not None:
        existing = store.get_db_config(payload["id"])
        if existing and (not payload.get("password") or payload["password"].startswith(existing.get("password_masked", "")[:4])):
            secret = store.get_db_secret(payload["id"])
            if secret and "password" in secret:
                payload["password"] = secret["password"]
            else:
                payload.pop("password", None)
    saved = store.save_db_config(payload)
    record(A_CONFIG_CHANGE, user["uid"], user.get("username"), {
        "type": "db", "action": "update" if req.id else "create",
        "id": saved.get("id"), "name": saved.get("name"), "host": saved.get("host"),
        "port": saved.get("port"), "username": saved.get("username"), "is_default": saved.get("is_default"),
    })
    return saved

@router.delete("/db/{cfg_id}")
def delete_db(cfg_id: int, user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not store.delete_db_config(cfg_id):
        raise HTTPException(404, "DB config not found")
    record(A_CONFIG_CHANGE, user["uid"], user.get("username"),
           {"type": "db", "action": "delete", "id": cfg_id})
    return {"ok": True}

# ---------- Test Connection ----------
@router.post("/llm/test")
def test_llm(req: LLMConfigSave, user: dict = Depends(get_current_user)):
    """测试连接用真实 key（按 provider 分发）"""
    from app.core.factories import build_llm_from
    api_key = req.api_key
    # 若是编辑且传了掩码串，取真 key
    if req.id and api_key and "****" in api_key:
        secret = store.get_llm_secret(req.id)
        api_key = secret["api_key"] if secret else api_key
    try:
        llm = build_llm_from(
            provider=req.provider,
            model_name=req.model_name,
            api_key=api_key,
            base_url=req.base_url,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
        llm.invoke("hi")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))

@router.post("/db/test")
def test_db(req: DBConfigSave, user: dict = Depends(get_current_user)):
    """测试连接用真实 password"""
    from sqlalchemy import create_engine, text
    from app.core.factories import mysql_engine_options
    password = req.password
    if req.id and password and "****" in password:
        secret = store.get_db_secret(req.id)
        password = secret["password"] if secret else password
    uri = f"{req.db_type}+pymysql://{req.username}:{password}@{req.host}:{req.port}/?charset={req.charset}"
    try:
        engine = create_engine(uri, **mysql_engine_options())
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))
