# app/core/store.py —— 加密版 JSON 存储：密文落盘、脱敏回显、upsert、默认互斥、delete、旧数据自动迁移
from __future__ import annotations
import json
from pathlib import Path
from threading import Lock
from copy import deepcopy
from typing import Optional
from app.meta import crypto

# 用 __file__ 推导绝对路径，避免从不同工作目录启动时配置分裂
_BASE_DIR = Path(__file__).resolve().parents[2]  # backend/
_DATA_DIR = _BASE_DIR / "data"
_LLM_FILE = _DATA_DIR / "llm_configs.json"
_DB_FILE  = _DATA_DIR / "db_configs.json"
_lock = Lock()

def _ensure_dir(): _DATA_DIR.mkdir(parents=True, exist_ok=True)

def _load(path: Path) -> list[dict]:
    if not path.exists(): return []
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f) or []
    except Exception: return []

def _save(path: Path, data: list[dict]):
    _ensure_dir()
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

def _next_id(items: list[dict]) -> int:
    return max((it.get("id", 0) for it in items), default=0) + 1

# ---------- 自动迁移：首次加载时把明文 key/密码加密，清掉磁盘明文 ----------
def _migrate_llm(items: list[dict]) -> bool:
    changed = False
    for it in items:
        if "api_key_enc" not in it and "api_key" in it:
            it["api_key_enc"] = crypto.encrypt(it.pop("api_key"))
            changed = True
    return changed

def _migrate_db(items: list[dict]) -> bool:
    changed = False
    for it in items:
        if "password_enc" not in it and "password" in it:
            it["password_enc"] = crypto.encrypt(it.pop("password"))
            changed = True
    return changed

def _load_and_migrate(path: Path, migrate_fn) -> list[dict]:
    items = _load(path)
    if migrate_fn(items):
        _save(path, items)
    return items

# ---------- 脱敏 view ----------
def _llm_view(o: dict) -> dict:
    v = {k: o[k] for k in ("id", "name", "provider", "base_url", "model_name", "temperature", "max_tokens", "is_default") if k in o}
    plain = crypto.decrypt(o["api_key_enc"]) if o.get("api_key_enc") else ""
    v["api_key_masked"] = crypto.mask(plain)
    return v

def _db_view(o: dict) -> dict:
    v = {k: o[k] for k in ("id", "name", "db_type", "host", "port", "username", "charset", "default_schema", "is_default") if k in o}
    plain = crypto.decrypt(o["password_enc"]) if o.get("password_enc") else ""
    v["password_masked"] = crypto.mask(plain)
    return v

# ========== LLM Config ==========
def list_llm_configs() -> list[dict]:
    with _lock:
        items = _load_and_migrate(_LLM_FILE, _migrate_llm)
        return [_llm_view(it) for it in items]

def get_llm_config(cfg_id: int) -> Optional[dict]:
    with _lock:
        items = _load_and_migrate(_LLM_FILE, _migrate_llm)
        it = next((x for x in items if x.get("id") == cfg_id), None)
        return _llm_view(it) if it else None

def get_llm_secret(cfg_id: int) -> Optional[dict]:
    """返回含真实 api_key 的完整配置（仅内部使用，不暴露给前端）"""
    with _lock:
        items = _load_and_migrate(_LLM_FILE, _migrate_llm)
        it = next((x for x in items if x.get("id") == cfg_id), None)
        if not it: return None
        result = deepcopy(it)
        if "api_key_enc" in result:
            result["api_key"] = crypto.decrypt(result.pop("api_key_enc"))
        return result

def save_llm_config(cfg: dict) -> dict:
    with _lock:
        items = _load_and_migrate(_LLM_FILE, _migrate_llm)
        # upsert
        idx = next((i for i, x in enumerate(items) if x.get("id") == cfg.get("id")), None)
        # 默认互斥
        if cfg.get("is_default"):
            for x in items: x["is_default"] = False
        # 加密
        if "api_key" in cfg:
            cfg["api_key_enc"] = crypto.encrypt(cfg.pop("api_key"))
        if idx is not None:
            items[idx] = cfg
        else:
            cfg["id"] = _next_id(items)
            items.append(cfg)
        _save(_LLM_FILE, items)
        return _llm_view(cfg)

def delete_llm_config(cfg_id: int) -> bool:
    with _lock:
        items = _load_and_migrate(_LLM_FILE, _migrate_llm)
        new_items = [x for x in items if x.get("id") != cfg_id]
        if len(new_items) == len(items): return False
        _save(_LLM_FILE, new_items)
        return True

# ========== DB Config ==========
def list_db_configs() -> list[dict]:
    with _lock:
        items = _load_and_migrate(_DB_FILE, _migrate_db)
        return [_db_view(it) for it in items]

def get_db_config(cfg_id: int) -> Optional[dict]:
    with _lock:
        items = _load_and_migrate(_DB_FILE, _migrate_db)
        it = next((x for x in items if x.get("id") == cfg_id), None)
        return _db_view(it) if it else None

def get_db_secret(cfg_id: int) -> Optional[dict]:
    """返回含真实 password 的完整配置（仅内部使用）"""
    with _lock:
        items = _load_and_migrate(_DB_FILE, _migrate_db)
        it = next((x for x in items if x.get("id") == cfg_id), None)
        if not it: return None
        result = deepcopy(it)
        if "password_enc" in result:
            result["password"] = crypto.decrypt(result.pop("password_enc"))
        return result

def save_db_config(cfg: dict) -> dict:
    with _lock:
        items = _load_and_migrate(_DB_FILE, _migrate_db)
        idx = next((i for i, x in enumerate(items) if x.get("id") == cfg.get("id")), None)
        if cfg.get("is_default"):
            for x in items: x["is_default"] = False
        if "password" in cfg:
            cfg["password_enc"] = crypto.encrypt(cfg.pop("password"))
        if idx is not None:
            items[idx] = cfg
        else:
            cfg["id"] = _next_id(items)
            items.append(cfg)
        _save(_DB_FILE, items)
        return _db_view(cfg)

def delete_db_config(cfg_id: int) -> bool:
    with _lock:
        items = _load_and_migrate(_DB_FILE, _migrate_db)
        new_items = [x for x in items if x.get("id") != cfg_id]
        if len(new_items) == len(items): return False
        _save(_DB_FILE, new_items)
        return True
