"""数据库 Agent 工具（tools/agent_tools.py）单测：SQL 安全校验 + 输出格式。

query_mysql 的成功路径通过 monkeypatch pandas.read_sql 模拟，不连真实数据库。
"""
import json

import pandas as pd
import pytest

from app.tools.agent_tools import make_tools, _to_md
from app.core.timeouts import MYSQL_MAX_EXECUTION_TIME_MS


def _q(sql: str):
    """直接调用 query_mysql 工具（engine 用 None：校验失败路径不会触库）。"""
    tools = {t.name: t for t in make_tools(engine=None)}
    return tools["query_mysql"].invoke({"sql": sql})


# ── SQL 安全校验（触库之前返回）──────────────────────────────────────────────
def test_rejects_write_statements():
    for sql in ("DROP TABLE users", "delete from t", "UPDATE t SET a=1",
                "INSERT INTO t VALUES (1)", "ALTER TABLE t", "TRUNCATE t",
                "CREATE TABLE t (id int)", "GRANT SELECT ON t TO u", "revoke all on t"):
        assert "仅允许 SELECT" in _q(sql), sql


def test_rejects_non_select_start():
    assert "必须以 SELECT 开头" in _q("show tables")
    assert "必须以 SELECT 开头" in _q("WITH x AS (SELECT 1) SELECT * FROM x")


def test_rejects_bare_dash_schema():
    out = _q("SELECT * FROM share-order.t")
    assert "反引号" in out and "share-order" in out


def test_allows_backticked_dash_schema():
    # 反引号包裹的横杠库名能通过校验；随后进入执行（engine=None → 报执行错误而非校验错误）
    out = _q("SELECT * FROM `share-order`.`orders`")
    assert "SQL 执行错误" in out


# ── 成功路径（mock pandas.read_sql）──────────────────────────────────────────
def _df():
    return pd.DataFrame({"city": ["北京", "上海"], "sales": [100, 200]})


def test_query_mysql_success_with_limit(monkeypatch):
    seen = {}
    def fake_read_sql(sql, engine):
        seen["sql"] = sql
        return _df()
    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    out = _q("SELECT * FROM t")
    assert "LIMIT 500" in seen["sql"]            # 自动追加 limit
    assert "| 北京 |" in out                      # markdown 表格
    machine = json.loads(out.split("<!--TABLE:")[1].rstrip("-->"))
    assert machine["columns"] == ["city", "sales"] and machine["rows"] == [["北京", 100], ["上海", 200]]


def test_query_mysql_keeps_existing_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(pd, "read_sql", lambda sql, engine: seen.setdefault("sql", sql) or _df())
    _q("SELECT * FROM t LIMIT 10")
    assert "LIMIT 10" in seen["sql"] and "LIMIT 500" not in seen["sql"]


def test_query_mysql_sql_error_friendly(monkeypatch):
    def boom(sql, engine):
        raise RuntimeError("syntax error near 'x'")
    monkeypatch.setattr(pd, "read_sql", boom)
    out = _q("SELECT * FROM t")
    assert "SQL 执行错误" in out and "syntax error" in out


def test_query_mysql_sets_mysql_server_execution_timeout(monkeypatch):
    seen = {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def exec_driver_sql(self, statement):
            seen["statement"] = statement

    class Engine:
        def connect(self):
            return Connection()

    def fake_read_sql(sql, conn):
        seen["connection"] = conn
        return _df()

    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    tools = {t.name: t for t in make_tools(engine=Engine())}
    tools["query_mysql"].invoke({"sql": "SELECT * FROM t"})

    assert seen["statement"] == f"SET SESSION max_execution_time = {MYSQL_MAX_EXECUTION_TIME_MS}"
    assert isinstance(seen["connection"], Connection)


def test_query_mysql_timeout_has_machine_readable_marker(monkeypatch):
    def timeout(*_args):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(pd, "read_sql", timeout)
    out = _q("SELECT * FROM t")
    assert out.startswith("[TOOL_TIMEOUT] query_mysql")


def test_query_mysql_invalidates_timed_out_connection(monkeypatch):
    seen = {"invalidated": None}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def exec_driver_sql(self, _statement):
            return None

        def invalidate(self, exc):
            seen["invalidated"] = exc

    class Engine:
        def connect(self):
            return Connection()

    def timeout(*_args):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(pd, "read_sql", timeout)
    tools = {t.name: t for t in make_tools(engine=Engine())}
    out = tools["query_mysql"].invoke({"sql": "SELECT * FROM t"})

    assert out.startswith("[TOOL_TIMEOUT]")
    assert isinstance(seen["invalidated"], TimeoutError)


# ── make_chart 工具 ───────────────────────────────────────────────────────────
def test_make_chart_pie():
    tools = {t.name: t for t in make_tools(engine=None)}
    out = tools["make_chart"].invoke({"chart_type": "pie", "title": "占比", "x_labels": [], "series": [{"name": "A", "value": 3}]})
    assert "<!--CHART:" in out
    opt = json.loads(out.split("<!--CHART:")[1].split("-->")[0])
    assert opt["series"][0]["type"] == "pie"


def test_make_chart_bar():
    tools = {t.name: t for t in make_tools(engine=None)}
    out = tools["make_chart"].invoke({"chart_type": "line", "title": "趋势", "x_labels": ["1月"], "series": [{"name": "s", "data": [1]}]})
    opt = json.loads(out.split("<!--CHART:")[1].split("-->")[0])
    assert opt["xAxis"]["data"] == ["1月"] and opt["series"][0]["type"] == "line"


def test_make_tools_registers_core_tools():
    names = [t.name for t in make_tools(engine=None)]
    assert names[:5] == ["list_schemas", "get_schema", "get_table_schema", "query_mysql", "make_chart"]


# ── 长工具输出压缩 ────────────────────────────────────────────────────────────
def test_query_mysql_compresses_long_output(monkeypatch):
    # 600 行结果 → markdown 只展示 50 行 + 省略提示；machine rows 仍 200（前端表格完整）
    monkeypatch.setattr(pd, "read_sql", lambda sql, engine: pd.DataFrame({"idx": range(600)}))
    tools = {t.name: t for t in make_tools(engine=None)}
    out = tools["query_mysql"].invoke({"sql": "SELECT * FROM t"})
    assert "已省略" in out
    machine = json.loads(out.split("<!--TABLE:")[1].rstrip("-->"))
    assert len(machine["rows"]) == 200


def test_query_mysql_short_output_not_omitted(monkeypatch):
    monkeypatch.setattr(pd, "read_sql", lambda sql, engine: pd.DataFrame({"idx": range(10)}))
    tools = {t.name: t for t in make_tools(engine=None)}
    out = tools["query_mysql"].invoke({"sql": "SELECT * FROM t"})
    assert "已省略" not in out
    assert "| 9 |" in out
