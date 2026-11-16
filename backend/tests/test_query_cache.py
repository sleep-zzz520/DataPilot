"""Query result cache: TTL/LRU, isolation, and tool integration."""
import pandas as pd

from app.db import query_cache
from app.db.query_cache import QueryResultCache, cacheable_sql
from app.tools.agent_tools import make_tools


def test_cache_ttl_lru_and_namespace():
    cache = QueryResultCache(ttl_seconds=10, max_entries=2)
    assert cache.get("missing") is None
    cache.set("a", "A")
    cache.set("b", "B")
    cache.set("c", "C")  # evicts a
    assert cache.get("a") is None and cache.get("b") == "B" and cache.get("c") == "C"
    assert cache.make_key("user:1", "SELECT 1") != cache.make_key("user:2", "SELECT 1")
    assert cacheable_sql("SELECT NOW()") is False
    assert cacheable_sql("SELECT * FROM t") is True


def test_cache_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(query_cache.time, "monotonic", lambda: now[0])
    cache = QueryResultCache(ttl_seconds=5)
    cache.set("k", "v")
    now[0] = 104.9
    assert cache.get("k") == "v"
    now[0] = 105.0
    assert cache.get("k") is None


def test_query_mysql_cache_hit_and_user_isolation(monkeypatch):
    query_cache.get_query_cache().clear()
    seen = []

    def fake_read_sql(sql, engine):
        seen.append(sql)
        return pd.DataFrame({"x": [1]})

    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    first = make_tools(None, cache_scope="user:1:db:1")
    second = make_tools(None, cache_scope="user:1:db:1")
    other = make_tools(None, cache_scope="user:2:db:1")

    a = {t.name: t for t in first}["query_mysql"].invoke({"sql": "SELECT x FROM t"})
    b = {t.name: t for t in second}["query_mysql"].invoke({"sql": "SELECT x FROM t"})
    c = {t.name: t for t in other}["query_mysql"].invoke({"sql": "SELECT x FROM t"})
    assert a == b and c == b
    assert len(seen) == 2


def test_query_mysql_does_not_cache_volatile_or_failed(monkeypatch):
    query_cache.get_query_cache().clear()
    seen = []
    fail = [True]

    def fake_read_sql(sql, engine):
        seen.append(sql)
        if fail[0]:
            fail[0] = False
            raise RuntimeError("boom")
        return pd.DataFrame({"x": [1]})

    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    tools = {t.name: t for t in make_tools(None, cache_scope="user:1:db:1")}
    error = tools["query_mysql"].invoke({"sql": "SELECT x FROM t"})
    ok = tools["query_mysql"].invoke({"sql": "SELECT x FROM t"})
    again = tools["query_mysql"].invoke({"sql": "SELECT x FROM t"})
    assert "SQL 执行错误" in error
    assert ok == again and len(seen) == 2

    tools["query_mysql"].invoke({"sql": "SELECT NOW() FROM t"})
    tools["query_mysql"].invoke({"sql": "SELECT NOW() FROM t"})
    assert len(seen) == 4


def test_file_query_cache_uses_path_fingerprint(tmp_path, monkeypatch):
    from app.tools import file_tool

    query_cache.get_query_cache().clear()
    source = tmp_path / "sales.csv"
    source.write_text("region,amount\neast,1\n", encoding="utf-8")
    calls = []

    def fake_run(_fn, path, sql, timeout_seconds):
        calls.append(sql)
        return ("| region | amount |\n|---|---|\n| east | 1 |\n"
                '<!--TABLE:{"columns":["region","amount"],"rows":[["east",1]],'
                '"executed_sql":"SELECT * FROM df LIMIT 500","source":"df",'
                '"row_count_total":1,"truncated":false}-->'), 1

    monkeypatch.setattr(file_tool, "run_in_process", fake_run)
    tools = {t.name: t for t in file_tool.make_file_tools(
        {"sales.csv": pd.DataFrame({"region": ["east"], "amount": [1]})},
        file_paths={"sales.csv": str(source)}, cache_scope="user:1:db:1",
    )}
    first = tools["query_file"].invoke({"file": "sales.csv", "sql": "SELECT * FROM df"})
    second = tools["query_file"].invoke({"file": "sales.csv", "sql": "SELECT * FROM df"})
    assert first == second and len(calls) == 1
