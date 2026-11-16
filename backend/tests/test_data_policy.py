"""Column masking and sensitive-table access policy."""
import pandas as pd

from app.agent.analysis_plan import AnalysisPlan
from app.agent.hitl import assess_confirmation
from app.audit import list_audit
from app.security.data_policy import DataPolicy, load_data_policy, mask_value
from app.semantic.catalog import SemanticCatalog
from app.tools.agent_tools import make_tools
from app.tools.file_tool import make_file_tools


def _policy(**kwargs):
    return DataPolicy(frozenset(kwargs.get("tables", ())),
                      frozenset(kwargs.get("columns", ())),
                      kwargs.get("patterns", True))


def test_catalog_sensitive_marks_drive_policy():
    catalog = SemanticCatalog.model_validate({
        "tables": [{
            "name": "share-order.customers", "sensitive": True,
            "columns": [{"name": "phone", "sensitive": True}, {"name": "region"}],
        }],
    })
    policy = load_data_policy(catalog)
    assert {"share-order.customers", "customers"} <= policy.sensitive_tables
    assert "phone" in policy.sensitive_columns
    assert policy.is_sensitive_column("contact_phone")
    assert not policy.is_sensitive_column("region")


def test_mask_value_keeps_non_reversible_shape():
    assert mask_value("alice@example.com") == "a***@example.com"
    assert mask_value("13812345678") == "138****5678"
    assert mask_value("张三") == "***"
    assert mask_value(None) == ""


def test_mask_dataframe_masks_pii_and_row_level_numeric():
    df = pd.DataFrame({
        "name": ["张三", "李四"],
        "phone": ["13812345678", "13987654321"],
        "salary": [1000, 2000],
        "region": ["华东", "华北"],
    })
    masked, columns = _policy(columns={"phone", "name"}).mask_dataframe(df)
    assert set(columns) == {"name", "phone", "salary"}
    assert masked["phone"].tolist() == ["138****5678", "139****4321"]
    assert masked["name"].tolist() == ["***", "***"]
    assert masked["salary"].tolist() == ["***", "***"]
    assert masked["region"].tolist() == ["华东", "华北"]


def test_numeric_aggregate_aliases_stay_usable():
    df = pd.DataFrame({"avg_salary": [12000, 13000], "total": [1, 2]})
    masked, columns = _policy(columns={"avg_salary"}).mask_dataframe(df)
    assert columns == []
    assert masked["avg_salary"].tolist() == [12000, 13000]


def test_single_row_numeric_sensitive_values_are_masked():
    one_row = pd.DataFrame({"salary": [12000]})
    masked, columns = _policy(columns={"salary"}).mask_dataframe(one_row)
    assert columns == ["salary"] and masked["salary"].tolist() == ["***"]
    assert one_row["salary"].tolist() == [12000]


def test_single_row_numeric_sensitive_values_never_reach_tool_output(isolated_storage, monkeypatch):
    df = pd.DataFrame({"salary": [123456], "phone": [13812345678]})
    monkeypatch.setattr(pd, "read_sql", lambda sql, engine: df)
    tools = {t.name: t for t in make_tools(engine=None, data_policy=_policy())}
    mysql_out = tools["query_mysql"].invoke({"sql": "SELECT salary, phone FROM employees LIMIT 1"})
    file_tools = {t.name: t for t in make_file_tools({"employees.csv": df}, data_policy=_policy())}
    file_out = file_tools["query_file"].invoke({
        "file": "employees.csv", "sql": "SELECT salary, phone FROM df LIMIT 1",
    })
    for out in (mysql_out, file_out):
        assert "123456" not in out and "13812345678" not in out
        assert "***" in out


def test_query_alias_of_sensitive_column_is_resolved():
    policy = _policy(columns={"phone"})
    assert "contact" in policy.sensitive_columns_for_sql(
        "SELECT phone AS contact FROM customers", dialect="mysql",
    )
    assert "contact" in policy.sensitive_columns_for_sql(
        "SELECT phone AS contact FROM df", dialect="duckdb",
        source_columns={"phone"},
    )
    assert policy.sensitive_columns_for_sql(
        "SELECT AVG(salary) AS pay FROM customers", dialect="mysql",
    ) == set()


def test_pattern_only_alias_is_masked_without_catalog_entry():
    policy = _policy()
    assert "contact" in policy.sensitive_columns_for_sql(
        "SELECT phone AS contact FROM customers", dialect="mysql",
    )


def test_sensitive_table_detection_and_cte_skip():
    policy = _policy(tables={"customers"})
    hits = policy.sensitive_tables_in_sql(
        "SELECT * FROM `share-order`.customers c JOIN orders o ON c.id=o.cid",
        default_schema="share-order",
    )
    assert hits == {"share-order.customers"}
    assert policy.sensitive_tables_in_sql(
        "SELECT * FROM (SELECT * FROM customers) x", default_schema="share-order"
    ) == {"share-order.customers"}


def test_sensitive_table_requires_approval_and_audits(isolated_storage, monkeypatch):
    seen = {}

    def fake_read(sql, engine):
        seen["read"] = True
        return pd.DataFrame({"id": [1]})

    monkeypatch.setattr(pd, "read_sql", fake_read)
    policy = _policy(tables={"customers"})
    ctx = {"user_id": 7, "username": "alice", "session_id": "s1"}

    denied_tools = {t.name: t for t in make_tools(
        engine=None, audit_ctx=ctx, data_policy=policy, sensitive_approved=False,
    )}
    denied = denied_tools["query_mysql"].invoke({"sql": "SELECT * FROM customers"})
    assert "敏感表" in denied and "read" not in seen
    assert list_audit(action="sensitive_access")[0]["detail"]["action"] == "denied"

    allowed_tools = {t.name: t for t in make_tools(
        engine=None, audit_ctx=ctx, data_policy=policy, sensitive_approved=True,
    )}
    allowed = allowed_tools["query_mysql"].invoke({"sql": "SELECT * FROM customers"})
    assert "| 1 |" in allowed
    assert list_audit(action="sensitive_access")[0]["detail"]["action"] == "allowed"


def test_mysql_result_masking_happens_before_model_output(isolated_storage, monkeypatch):
    df = pd.DataFrame({"phone": ["13812345678"], "region": ["华东"]})
    monkeypatch.setattr(pd, "read_sql", lambda sql, engine: df)
    tools = {t.name: t for t in make_tools(
        engine=None,
        audit_ctx={"user_id": 7, "username": "alice", "session_id": "s1"},
        data_policy=_policy(columns={"phone"}),
    )}
    out = tools["query_mysql"].invoke({"sql": "SELECT phone, region FROM customers"})
    assert "13812345678" not in out and "138****5678" in out
    assert list_audit(action="data_mask")[0]["detail"]["columns"] == ["phone"]


def test_file_result_masking_includes_aliases():
    df = pd.DataFrame({"phone": ["13812345678"], "region": ["华东"]})
    tools = {t.name: t for t in make_file_tools(
        {"客户.csv": df},
        data_policy=_policy(columns={"phone"}),
    )}
    out = tools["query_file"].invoke({
        "file": "客户.csv", "sql": "SELECT phone AS contact, region FROM df",
    })
    assert "13812345678" not in out and "138****5678" in out
    assert '"contact"' in out


def test_sensitive_table_adds_confirmation_reason():
    plan = AnalysisPlan(goal="统计客户数", data_sources=["customers"], steps=[
        {"id": "q", "title": "查询 customers", "kind": "query"},
    ])
    result = assess_confirmation("统计客户数", plan, sensitive_tables={"customers"})
    assert result["required"] is True
    assert "sensitive_data" in result["reasons"]
