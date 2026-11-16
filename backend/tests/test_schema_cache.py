"""Schema metadata cache: TTL, structured metadata and engine isolation."""
from app.db import schema_cache as cache_module
from app.db.schema_cache import SchemaCache

SCHEMA_SQL = (
    "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
    "WHERE SCHEMA_NAME NOT IN ('information_schema','mysql','performance_schema','sys') "
    "AND SCHEMA_NAME LIKE :p"
)
TABLES_SQL = (
    "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
    "WHERE TABLE_SCHEMA = :s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME"
)
COLUMNS_SQL = (
    "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, COLUMN_KEY "
    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=:s AND TABLE_NAME IN (:t0) "
    "ORDER BY TABLE_NAME, ORDINAL_POSITION"
)
COMMENTS_SQL = (
    "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
    "WHERE TABLE_SCHEMA=:s AND TABLE_TYPE='BASE TABLE' AND TABLE_NAME IN (:t0)"
)
KEYS_SQL = (
    "SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.STATISTICS "
    "WHERE TABLE_SCHEMA=:s AND TABLE_NAME IN (:t0) AND NON_UNIQUE=0 "
    "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, engine):
        self.engine = engine

    def execute(self, stmt, params=None):
        self.engine.queries.append(getattr(stmt, "text", stmt))
        text = getattr(stmt, "text", stmt)
        if text == SCHEMA_SQL:
            return _Result([("share-order",)])
        if text == TABLES_SQL:
            return _Result([("orders", "订单表")])
        if text == COLUMNS_SQL:
            return _Result([
                ("orders", "id", "bigint", "主键", "PRI"),
                ("orders", "sales_amount", "decimal(18,2)", "销售额", ""),
            ])
        if text == COMMENTS_SQL:
            return _Result([("orders", "订单表")])
        if text == KEYS_SQL:
            return _Result([("orders", "PRIMARY", "id")])
        return _Result([])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Url:
    def render_as_string(self, hide_password=True):
        return "mysql://user:***@db/share-order"


class _Engine:
    def __init__(self):
        self.url = _Url()
        self.queries = []

    def connect(self):
        return _Conn(self)


def test_structured_columns_and_unique_keys():
    cache = SchemaCache(ttl_seconds=300)
    engine = _Engine()
    meta = cache.get_columns(engine, "share-order", ["orders"])
    assert meta["orders"]["name"] == "share-order.orders"
    assert meta["orders"]["comment"] == "订单表"
    assert [column["name"] for column in meta["orders"]["columns"]] == ["id", "sales_amount"]
    assert meta["orders"]["unique_keys"] == [["id"]]


def test_cache_reuses_table_and_column_metadata():
    cache = SchemaCache(ttl_seconds=300)
    engine = _Engine()
    first = cache.get_columns(engine, "share-order", ["orders"])
    calls = len(engine.queries)
    second = cache.get_columns(engine, "share-order", ["orders"])
    assert first == second
    assert len(engine.queries) == calls

    tables = cache.get_tables(engine, "share-order")
    assert tables == [{"name": "share-order.orders", "comment": "订单表"}]
    assert cache.get_tables(engine, "share-order") == tables


def test_cache_expires_and_can_be_cleared(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: clock["now"])
    cache = SchemaCache(ttl_seconds=10)
    engine = _Engine()
    cache.get_columns(engine, "share-order", ["orders"])
    calls = len(engine.queries)
    clock["now"] = 105.0
    cache.get_columns(engine, "share-order", ["orders"])
    assert len(engine.queries) == calls
    clock["now"] = 111.0
    cache.get_columns(engine, "share-order", ["orders"])
    assert len(engine.queries) > calls
    cache.clear()
    assert cache.get_columns(engine, "share-order", ["orders"])
