"""Semantic catalog loading and owner-edited metadata contracts."""
from pathlib import Path

from app.semantic.catalog import SemanticCatalog, load_semantic_catalog

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "semantic_catalog.example.json"


def test_example_catalog_parses_tables_metrics_joins_and_verified_queries():
    catalog = load_semantic_catalog(EXAMPLE)
    assert catalog.version == "1"
    orders = catalog.find_table("share-order.orders")
    assert orders is not None
    assert orders.effective_schema == "share-order"
    assert orders.table_name == "orders"
    assert {column.name for column in orders.columns} >= {"sales_amount", "refund_amount", "region"}
    assert catalog.metrics[0].contract_id == "sales.net_sales"
    assert catalog.joins[0].left == "share-order.orders"
    assert catalog.verified_queries[0].tables == ["share-order.orders"]


def test_catalog_supports_string_columns_and_schema_alias():
    catalog = SemanticCatalog.model_validate({
        "tables": [{"name": "orders", "schema": "share-order", "columns": ["id", "amount"]}]
    })
    table = catalog.find_table("orders")
    assert table.effective_schema == "share-order"
    assert table.column_names == ["id", "amount"]


def test_malformed_catalog_fails_open_to_empty(tmp_path):
    path = tmp_path / "semantic.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_semantic_catalog(path).is_empty()


def test_missing_catalog_is_empty(tmp_path):
    assert load_semantic_catalog(tmp_path / "missing.json").is_empty()
