"""认证核心逻辑 + /api/auth 接口集成测试。"""
import time

import pytest
import jwt

from app.auth import hash_password, verify_password, create_token, decode_token


# ── 纯函数：密码哈希 ──────────────────────────────────────────────────────────
def test_hash_password_format():
    h = hash_password("secret123")
    assert h.count("$") == 1
    salt, digest = h.split("$")
    assert len(salt) == 32 and len(digest) == 64  # 16字节盐 hex + sha256 hex


def test_hash_password_random_salt():
    assert hash_password("same") != hash_password("same")  # 每次盐不同


def test_verify_password_ok():
    assert verify_password("secret123", hash_password("secret123"))


def test_verify_password_wrong():
    assert not verify_password("wrong", hash_password("secret123"))


def test_verify_password_malformed():
    assert not verify_password("x", "no-dollar-sign")
    assert not verify_password("x", "")


# ── 纯函数：JWT ───────────────────────────────────────────────────────────────
def test_create_and_decode_token(isolated_storage):
    token = create_token(7, "alice", "admin")
    payload = decode_token(token)
    assert payload["uid"] == 7
    assert payload["username"] == "alice"
    assert payload["role"] == "admin"
    assert payload["exp"] > int(time.time())


def test_decode_token_invalid(isolated_storage):
    assert decode_token("not-a-jwt") is None
    assert decode_token("") is None


def test_decode_token_wrong_signature(isolated_storage):
    token = create_token(1, "alice")
    forged = token.rsplit(".", 1)[0] + "." + "AAAA"  # 篡改签名
    assert decode_token(forged) is None


def test_decode_token_expired(isolated_storage):
    # 直接用 jwt 库签发一个已过期 token（secret 由 fixture 指向 tmp，与 decode 一致）
    import app.auth as auth_mod
    secret = auth_mod._load_secret()
    expired = jwt.encode({"uid": 1, "exp": int(time.time()) - 100}, secret, algorithm="HS256")
    assert decode_token(expired) is None


# ── API 集成：注册 / 登录 / me ────────────────────────────────────────────────
def test_register_first_user_is_admin(client):
    r = client.post("/api/auth/register", json={"username": "boss", "password": "pass1234"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin" and body["token"]
    assert decode_token(body["token"])["username"] == "boss"


def test_register_duplicate_name(client):
    client.post("/api/auth/register", json={"username": "alice", "password": "pass1234"})
    r = client.post("/api/auth/register", json={"username": "alice", "password": "other123"})
    assert r.status_code == 400


def test_register_invalid_input(client):
    assert client.post("/api/auth/register", json={"username": "a", "password": "pass1234"}).status_code == 400
    assert client.post("/api/auth/register", json={"username": "bob", "password": "123"}).status_code == 400


def test_login_ok_and_wrong(client):
    client.post("/api/auth/register", json={"username": "alice", "password": "pass1234"})
    ok = client.post("/api/auth/login", json={"username": "alice", "password": "pass1234"})
    assert ok.status_code == 200 and ok.json()["role"] == "admin"
    bad = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert bad.status_code == 401


def test_me_requires_token(client):
    assert client.get("/api/auth/me").status_code == 401
    r = client.post("/api/auth/register", json={"username": "alice", "password": "pass1234"})
    token = r.json()["token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


def _register(client, name, pwd="pass1234"):
    return client.post("/api/auth/register", json={"username": name, "password": pwd}).json()["token"]


# ── API 集成：用户管理（管理员）──────────────────────────────────────────────
def test_user_role_management(client):
    admin_token = _register(client, "admin1")
    user_token = _register(client, "worker1")  # 第二个用户 → user
    admin_h = {"Authorization": f"Bearer {admin_token}"}
    user_h = {"Authorization": f"Bearer {user_token}"}

    # 非管理员不能看用户列表
    assert client.get("/api/auth/users", headers=user_h).status_code == 403
    users = client.get("/api/auth/users", headers=admin_h)
    assert users.status_code == 200
    names = {u["username"] for u in users.json()}
    assert names == {"admin1", "worker1"}
    assert all("password" not in u and "password_hash" not in u for u in users.json())

    # 管理员不能改自己的角色
    admin = next(u for u in users.json() if u["username"] == "admin1")
    assert client.put(f"/api/auth/users/{admin['id']}", json={"role": "user"},
                      headers=admin_h).status_code == 400

    # 已签发的 token 也使用当前账号角色
    worker = next(u for u in users.json() if u["username"] == "worker1")
    assert client.put(f"/api/auth/users/{worker['id']}", json={"role": "admin"},
                      headers=admin_h).status_code == 200
    assert client.get("/api/auth/users", headers=user_h).status_code == 200
    assert client.put(f"/api/auth/users/{worker['id']}", json={"role": "user"},
                      headers=admin_h).status_code == 200
    assert client.get("/api/auth/users", headers=user_h).status_code == 403


def test_change_password(client):
    token = _register(client, "alice")
    h = {"Authorization": f"Bearer {token}"}
    # 原密码错误
    assert client.post("/api/auth/change-password",
                       json={"old_password": "wrong", "new_password": "newpass123"}, headers=h).status_code == 400
    # 新密码太短
    assert client.post("/api/auth/change-password",
                       json={"old_password": "pass1234", "new_password": "123"}, headers=h).status_code == 400
    ok = client.post("/api/auth/change-password",
                     json={"old_password": "pass1234", "new_password": "newpass123"}, headers=h)
    assert ok.status_code == 200
    # 新密码可登录
    assert client.post("/api/auth/login", json={"username": "alice", "password": "newpass123"}).status_code == 200


def test_delete_account(client, isolated_storage):
    token = _register(client, "ghost")
    h = {"Authorization": f"Bearer {token}"}
    assert client.delete("/api/auth/account", headers=h).status_code == 200
    # 账号已删：无法再登录，也不再出现在用户列表
    assert client.post("/api/auth/login", json={"username": "ghost", "password": "pass1234"}).status_code == 401
    assert client.get("/api/auth/users", headers=h).status_code == 401
