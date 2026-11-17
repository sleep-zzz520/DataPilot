"""Planner sees authoritative source columns before deciding whether to clarify."""
import json

import pytest
from langchain_core.messages import AIMessage

from app.api import chat_api


class SchemaAwarePlanner:
    def __init__(self):
        self.catalog = None

    def invoke(self, messages):
        prompt = messages[0].content
        self.catalog = json.loads(prompt.split("候选数据表：", 1)[1].split("\n用户问题：", 1)[0])
        loaded = [item for item in self.catalog if item.get("columns_status") == "loaded"]
        has_category = any(column["name"] == "category"
                           for item in loaded for column in item.get("columns", []))
        missing = bool(loaded) and not has_category
        return AIMessage(content=json.dumps({
            "goal": "按品类汇总", "data_sources": [item["name"] for item in self.catalog],
            "metrics": ["SUM(amount)", "SUM(quantity)"], "dimensions": ["sku.category"] if has_category else [],
            "steps": [{"id": "q", "title": "查询", "kind": "query"}],
            "clarification_needed": missing,
            "clarification_question": "缺少品类字段，是否使用其他维度？" if missing else None,
        }, ensure_ascii=False))


class Metadata:
    def __init__(self, category=True, fail=False):
        self.calls = []
        self.category = category
        self.fail = fail

    def get_columns(self, engine, schema, tables):
        self.calls.append((schema, tables))
        if self.fail:
            raise RuntimeError("metadata unavailable")
        return {table: {"name": f"{schema}.{table}", "columns": [
            {"name": column, "type": "int", "comment": "", "key": ""}
            for column in (["sku_id", "category"] if table == "sku" and self.category
                           else ["sku_id"] if table == "sku" else ["sku_id", "amount", "quantity"])
        ]} for table in tables}


@pytest.fixture
def sources(monkeypatch):
    def list_tables(engine, schema):
        assert schema == "share-retail"
        return [{"name": f"{schema}.{table}", "comment": ""} for table in ("sku", "sales_order_item")]
    monkeypatch.setattr(chat_api, "list_tables", list_tables)


def test_current_database_columns_reach_planner_before_hitl(sources, monkeypatch, isolated_storage):
    metadata = Metadata()
    monkeypatch.setattr(chat_api, "get_schema_cache", lambda: metadata)
    monkeypatch.setattr(chat_api, "build_memory_context", lambda *a, **kw: "")
    monkeypatch.setattr(chat_api, "_ensure_loaded", lambda *a: None)
    monkeypatch.setattr(chat_api, "get_history", lambda *a: [])
    planner = SchemaAwarePlanner()
    req = chat_api.ChatRequest(message="按商品品类汇总订单明细的 amount 和 quantity", db_config_id=3, llm_config_id=1)
    result = chat_api._resolve_plan(req, "catalog-test", planner, object(), "share-retail",
                                    {"uid": 1, "username": "admin"})
    assert result["plan"].clarification_needed is False
    assert result["kind"] == "confirmation"  # Original high-cost approval remains intact.
    assert result["confirmation"]["mode"] == "approval"
    assert "metric_ambiguity" not in result["confirmation"]["reasons"]
    assert metadata.calls == [("share-retail", ["sku", "sales_order_item"])]
    assert {item["name"] for item in planner.catalog} == {"share-retail.sku", "share-retail.sales_order_item"}
    assert {c["name"] for c in planner.catalog[1]["columns"]} == {"sku_id", "amount", "quantity"}


def test_confirmed_missing_column_can_still_require_clarification(sources, monkeypatch):
    monkeypatch.setattr(chat_api, "get_schema_cache", lambda: Metadata(category=False))
    plan, _ = chat_api._build_plan(SchemaAwarePlanner(), object(), "按品类汇总", "share-retail", [], 1)
    assert plan.clarification_needed is True


def test_metadata_failure_is_unknown_not_empty_columns(sources, monkeypatch):
    monkeypatch.setattr(chat_api, "get_schema_cache", lambda: Metadata(fail=True))
    planner = SchemaAwarePlanner()
    chat_api._build_plan(planner, object(), "按品类汇总", "share-retail", [], 1)
    assert all(item["columns_status"] == "unavailable" for item in planner.catalog)
    assert all("columns" not in item for item in planner.catalog)


@pytest.mark.parametrize("engine", [None, object()])
def test_uploaded_columns_survive_absent_or_failed_database_discovery(monkeypatch, engine):
    def unavailable(*args):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(chat_api, "list_tables", unavailable)
    monkeypatch.setattr(chat_api, "get_upload", lambda fid, uid: {
        "name": "products.csv", "columns": [{"name": "category"}, {"name": "amount"}],
    })
    planner = SchemaAwarePlanner()
    chat_api._build_plan(planner, engine, "按品类汇总", None, ["owned-file"], 7)
    assert planner.catalog == [{"name": "products.csv", "comment": "上传文件",
                               "columns": [{"name": "category"}, {"name": "amount"}],
                               "columns_status": "loaded"}]


def test_large_catalog_bounds_metadata_without_claiming_unloaded_columns_missing(monkeypatch):
    monkeypatch.setattr(chat_api, "list_tables", lambda *a: [
        {"name": f"share-retail.table_{i}"} for i in range(40)
    ])
    metadata = Metadata()
    monkeypatch.setattr(chat_api, "get_schema_cache", lambda: metadata)
    planner = SchemaAwarePlanner()
    chat_api._build_plan(planner, object(), "查表", "share-retail", [], 1)
    assert len(metadata.calls[0][1]) == 32
    assert len(planner.catalog) == 40
    assert all(item["columns_status"] == "not_loaded" and "columns" not in item
               for item in planner.catalog[32:])
