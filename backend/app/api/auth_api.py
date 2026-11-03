"""注册 / 登录 / 当前用户接口。"""
from __future__ import annotations

import os
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.auth import hash_password, verify_password, create_token, decode_token
from app.audit import record, A_USER_ACTION
from app.persistence import (
    create_user, get_user_by_username, claim_orphan_data,
    list_users, update_user_role, update_password, delete_user,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _require_admin(user: dict) -> None:
    if user.get("role") != "admin":
        raise HTTPException(403, "仅管理员可操作")


def get_current_user(authorization: str = Header(default="")) -> dict:
    """FastAPI 依赖：解析 Bearer token，失败抛 401。返回当前账号及角色。"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    payload = decode_token(authorization[7:])
    if not payload:
        raise HTTPException(401, "登录已过期，请重新登录")
    account = get_user_by_username(payload.get("username", ""))
    if not account or account["id"] != payload.get("uid"):
        raise HTTPException(401, "账号不存在，请重新登录")
    return {**payload, "role": account["role"]}


class AuthReq(BaseModel):
    username: str
    password: str


@router.post("/register")
def register(req: AuthReq):
    username = req.username.strip()
    if len(username) < 2 or len(req.password) < 4:
        raise HTTPException(400, "用户名至少 2 位，密码至少 4 位")
    user = create_user(username, hash_password(req.password))
    if user is None:
        raise HTTPException(400, "用户名已存在")
    claim_orphan_data(user["id"])  # 第一个注册用户自动继承旧版无主数据
    record(A_USER_ACTION, user["id"], username, {"action": "register", "role": user["role"]})
    return {"ok": True, "token": create_token(user["id"], username, user["role"]), "username": username, "role": user["role"]}


@router.post("/login")
def login(req: AuthReq):
    user = get_user_by_username(req.username.strip())
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    claim_orphan_data(user["id"])  # 存量无主会话/上传归给本人，避免升级后历史会话不可见
    record(A_USER_ACTION, user["id"], user["username"], {"action": "login"})
    return {"ok": True, "token": create_token(user["id"], user["username"], user["role"]), "username": user["username"], "role": user["role"]}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"id": user["uid"], "username": user["username"], "role": user.get("role", "user")}


# ── 用户管理（仅管理员）──────────────────────────────────────────

@router.get("/users")
def api_list_users(user: dict = Depends(get_current_user)):
    """用户列表（不含密码）。"""
    _require_admin(user)
    return list_users()


class RoleReq(BaseModel):
    role: str  # admin / user


@router.put("/users/{user_id}")
def api_set_user_role(user_id: int, req: RoleReq, user: dict = Depends(get_current_user)):
    """提升/降级用户角色（管理员可操作，但不能改自己）。"""
    _require_admin(user)
    if user_id == user["uid"]:
        raise HTTPException(400, "不能修改自己的角色")
    if req.role not in ("admin", "user"):
        raise HTTPException(400, "角色只能是 admin 或 user")
    if not update_user_role(user_id, req.role):
        raise HTTPException(404, "用户不存在")
    record(A_USER_ACTION, user["uid"], user.get("username"),
           {"action": "set_role", "target_user_id": user_id, "role": req.role})
    return {"ok": True}


# ── 账号自助（登录用户）──────────────────────────────────────────

class ChangePwdReq(BaseModel):
    old_password: str
    new_password: str


@router.post("/change-password")
def api_change_password(req: ChangePwdReq, user: dict = Depends(get_current_user)):
    """修改密码：校验原密码后更新。"""
    stored = get_user_by_username(user["username"])
    if not stored or not verify_password(req.old_password, stored["password_hash"]):
        raise HTTPException(400, "原密码错误")
    if len(req.new_password) < 4:
        raise HTTPException(400, "新密码至少 4 位")
    update_password(user["uid"], hash_password(req.new_password))
    record(A_USER_ACTION, user["uid"], user.get("username"), {"action": "change_password"})
    return {"ok": True}


@router.delete("/account")
def api_delete_account(user: dict = Depends(get_current_user)):
    """注销账号：删除该用户全部会话、上传文件（含磁盘文件）。"""
    paths = delete_user(user["uid"])
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
    record(A_USER_ACTION, user["uid"], user.get("username"), {"action": "delete_account"})
    return {"ok": True}
