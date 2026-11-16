"""Query runtime metrics and slow-query audit."""
import pandas as pd
import pytest

from app.audit import list_audit
from app.db.query_runtime import QueryRuntimeMetrics, query_runtime, record_query_runtime
from app.tools.agent_tools import make_tools


@pytest.fixture(autouse=True)
def _reset_runtime():
    query_runtime.reset()
    yield
    query_runtime.reset()


def test_snapshot_aggregates_latency_and_errors():
    local = QueryRuntimeMetrics()
    local.record("mysql", 10, rows=3)
    local.record("mysql", 30, rows=5)
    local.record("file", 100, rows=1, ok=False)
    snap = local.snapshot()
    assert snap["total_queries"] == 3
    assert snap["errors"] == 1
    assert snap["latency_ms"]["max"] == 100
    assert snap["sources"]["mysql"]["total"] == 2
    assert snap["sources"]["mysql"]["avg_ms"] == 20
    assert snap["sources"]["file"]["errors"] == 1


def test_slow_query_writes_audit(isolated_storage, monkeypatch):
    monkeypatch.setattr("app.db.query_runtime.SLOW_QUERY_MS", 10)
    slow = record_query_runtime(
        "mysql", 25.5, rows=8,
        audit_ctx={"user_id": 7, "username": "alice", "session_id": "s1"},
        sql="SELECT * FROM t",
    )
    assert slow is True
    rows = list_audit(action="slow_query")
    assert len(rows) == 1
    detail = rows[0]["detail"]
    assert detail["source"] == "mysql" and detail["duration_ms"] == 25.5
    assert detail["rows"] == 8 and detail["session_id"] == "s1"


def test_fast_query_does_not_write_slow_audit(isolated_storage, monkeypatch):
    monkeypatch.setattr("app.db.query_runtime.SLOW_QUERY_MS", 1000)
    assert record_query_runtime(
        "mysql", 5, audit_ctx={"user_id": 7, "username": "alice"}, sql="SELECT 1"
    ) is False
    assert list_audit(action="slow_query") == []


def test_mysql_tool_records_duration_in_audit_and_metrics(isolated_storage, monkeypatch):
    monkeypatch.setattr("app.db.query_runtime.SLOW_QUERY_MS", 0)
    monkeypatch.setattr(pd, "read_sql", lambda sql, engine: pd.DataFrame({"n": [1, 2]}))
    tools = {t.name: t for t in make_tools(
        engine=None,
        audit_ctx={"user_id": 7, "username": "alice", "session_id": "s1"},
    )}
    out = tools["query_mysql"].invoke({"sql": "SELECT * FROM t"})
    assert "| 1 |" in out

    snap = query_runtime.snapshot()
    assert snap["total_queries"] == 1 and snap["slow_queries"] == 1
    audit = list_audit(action="sql_query")
    assert audit[0]["detail"]["source"] == "mysql"
    assert isinstance(audit[0]["detail"]["duration_ms"], (int, float))
    slow = list_audit(action="slow_query")
    assert len(slow) == 1 and slow[0]["detail"]["rows"] == 2
