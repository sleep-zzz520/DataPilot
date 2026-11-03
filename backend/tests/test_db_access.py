"""Database config grants through the actual HTTP entry points."""
import pytest
from sqlalchemy import create_engine, text

from app.api import chat_api


def register(client, name):
    response = client.post("/api/auth/register", json={"username": name, "password": "local-test"})
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    return headers, client.get("/api/auth/me", headers=headers).json()["id"]


@pytest.fixture
def accounts_and_databases(client):
    admin, _ = register(client, "admin")
    alice, alice_id = register(client, "alice")
    bob, bob_id = register(client, "bob")
    configs = []
    for name in ("orders", "inventory"):
        response = client.post("/api/config/db", headers=admin, json={
            "name": name, "db_type": "mysql", "host": "unused.invalid", "port": 3306,
            "username": "read_only", "password": "test-password", "default_schema": name,
        })
        assert response.status_code == 200
        configs.append(response.json())
    return admin, (alice, alice_id), (bob, bob_id), configs


def test_db_access_is_checked_before_engine_and_stream(client, accounts_and_databases, monkeypatch, tmp_path):
    admin, (alice, alice_id), (bob, bob_id), (orders, inventory) = accounts_and_databases
    for headers, uid, cfg, other in (
        (alice, alice_id, orders, inventory), (bob, bob_id, inventory, orders),
    ):
        assert client.get("/api/config/db", headers=headers).json() == []
        assert client.put(f"/api/config/db/{cfg['id']}/grants", headers=admin,
                          json={"user_ids": [uid]}).json() == {"user_ids": [uid]}
        assert client.get(f"/api/config/db/{other['id']}", headers=headers).status_code == 404
    assert len(client.get("/api/config/db", headers=admin).json()) == 2

    engines = {}
    for config in (orders, inventory):
        name = config["name"]
        engine = create_engine(f"sqlite:///{tmp_path / (name + '.sqlite')}")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE fixture (dataset TEXT)"))
            conn.execute(text("INSERT INTO fixture VALUES (:name)"), {"name": name})
        engines[name] = engine
    built = []

    def build_engine(config):
        built.append(config["name"])
        return engines[config["name"]]

    def list_tables(engine, schema):
        with engine.connect() as conn:
            return [{"name": conn.execute(text("SELECT dataset FROM fixture")).scalar_one()}]

    monkeypatch.setattr(chat_api, "build_engine", build_engine)
    monkeypatch.setattr(chat_api, "list_tables", list_tables)
    for headers, cfg, forbidden in ((alice, orders, inventory), (bob, inventory, orders)):
        visible = client.get("/api/config/db", headers=headers).json()
        assert [item["id"] for item in visible] == [cfg["id"]]
        assert "password_masked" not in visible[0]
        assert "password_masked" not in client.get(f"/api/config/db/{cfg['id']}", headers=headers).json()
        schema = client.get("/api/schema", headers=headers, params={"db_config_id": cfg["id"]})
        assert schema.json() == {"tables": [{"name": cfg["name"]}]}
        assert client.get("/api/schema", headers=headers,
                          params={"db_config_id": forbidden["id"]}).status_code == 404
        for path in ("/api/chat", "/api/chat/stream"):
            denied = client.post(path, headers=headers, json={
                "message": "SELECT * FROM fixture", "llm_config_id": 1, "db_config_id": forbidden["id"],
            })
            assert denied.status_code == 404
            assert denied.headers["content-type"] == "application/json"
    assert built == ["orders", "inventory"]
    assert client.get("/api/schema", params={"db_config_id": orders["id"]}).status_code == 401
    for engine in engines.values():
        engine.dispose()


def test_only_admin_can_manage_grants_and_revoke_access(client, accounts_and_databases):
    admin, (alice, alice_id), _, (orders, _) = accounts_and_databases
    config_url = f"/api/config/db/{orders['id']}"
    url = f"{config_url}/grants"
    assert client.get(url, headers=alice).status_code == 403
    assert client.put(url, headers=alice, json={"user_ids": [alice_id]}).status_code == 403
    assert client.post("/api/config/db/test", headers=alice, json=orders).status_code == 403
    assert client.put(url, headers=admin, json={"user_ids": [9999]}).status_code == 400
    assert client.put(url, headers=admin, json={"user_ids": [alice_id]}).status_code == 200
    edit = {**orders, "name": "renamed", "password": orders["password_masked"]}
    assert client.post("/api/config/db", headers=admin, json=edit).status_code == 200
    assert client.get(url, headers=admin).json() == {"user_ids": [alice_id]}
    assert client.get(config_url, headers=alice).json()["name"] == "renamed"
    assert client.put(url, headers=admin, json={"user_ids": []}).status_code == 200
    assert client.get(config_url, headers=alice).status_code == 404



def test_legacy_db_config_requires_explicit_grant(client, isolated_storage):
    legacy_config = isolated_storage / "db_configs.json"
    legacy_config.write_text('[{"id": 5, "name": "legacy", "password": "test-password"}]')
    admin, _ = register(client, "admin")
    alice, alice_id = register(client, "alice")

    assert client.get("/api/config/db/5", headers=admin).json()["name"] == "legacy"
    assert client.get("/api/config/db/5", headers=alice).status_code == 404
    assert client.get("/api/config/db/5/grants", headers=admin).json() == {"user_ids": []}
    assert client.put("/api/config/db/5/grants", headers=admin, json={"user_ids": [alice_id]}).status_code == 200
    assert client.get("/api/config/db/5", headers=alice).json()["name"] == "legacy"
