"""Exercise pandas -> SQLAlchemy MySQL -> real PyMySQL interpolation, without a server."""
import pandas as pd
import pymysql
import pytest
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.dialects.mysql.pymysql import MySQLDialect_pymysql
from sqlalchemy.pool import StaticPool

from app.tools.agent_tools import _read_mysql_with_timeout


class DriverConnection:
    encoding = "utf8"

    def __init__(self):
        self.statements = []

    def cursor(self):
        return DriverCursor(self)

    def rollback(self):
        pass

    def close(self):
        pass


class DriverCursor(pymysql.cursors.Cursor):
    # execute() and mogrify() remain the real driver implementation. Only wire I/O
    # and result retrieval are replaced; empty parameter interpolation still fails.
    def _query(self, query):
        self.connection.statements.append(query)
        self.description = None
        self.rowcount = 0
        self._rows = []
        if query.startswith("SELECT"):
            self.description = [("value", 253, None, None, None, None, None)]
            self._rows = [("ok",)]
            self.rowcount = 1
        self._result = None
        return self.rowcount

    def nextset(self):
        return None


def make_driver_engine():
    driver = DriverConnection()
    dialect = MySQLDialect_pymysql(dbapi=pymysql)
    dialect.server_version_info = (8, 0, 36)
    dialect.default_schema_name = "retail"
    engine = Engine(StaticPool(creator=lambda: driver), dialect,
                    make_url("mysql+pymysql://"))
    return engine, driver


@pytest.mark.parametrize("sql", [
    "SELECT DATE_FORMAT(order_date, '%Y-%m') AS month FROM sales_order",
    "SELECT category FROM sku WHERE category LIKE '%phone%'",
    "SELECT ':literal', '12:30', '100%', '%%', '%(name)s'",
    "SELECT amount % 10 FROM sales_order_item",
])
def test_mysql_query_preserves_percent_and_colon_literals(sql):
    engine, driver = make_driver_engine()
    try:
        result = _read_mysql_with_timeout(sql, engine, "retail")
        assert result.to_dict("records") == [{"value": "ok"}]
        assert driver.statements[-1] == sql
        assert any(s.startswith("SET SESSION max_execution_time") for s in driver.statements)
    finally:
        engine.dispose()


def test_regression_reproduces_original_empty_parameter_interpolation_failure():
    engine, _driver = make_driver_engine()
    try:
        with engine.connect() as conn:
            with pytest.raises(ValueError, match="unsupported format character"):
                pd.read_sql("SELECT DATE_FORMAT(order_date, '%Y-%m') FROM sales_order", conn)
    finally:
        engine.dispose()
