"""AST SQL policy: read-only, single statement, no dangerous constructs."""
import pytest

from app.db.sql_policy import SqlPolicyError, policy_message, validate_readonly_sql


def _code(sql, **kwargs):
    with pytest.raises(SqlPolicyError) as exc:
        validate_readonly_sql(sql, **kwargs)
    return exc.value.code


def test_select_gets_outer_limit_and_preserves_text():
    assert validate_readonly_sql("SELECT * FROM t") == "SELECT * FROM t LIMIT 500"
    assert validate_readonly_sql("SELECT * FROM t LIMIT 10") == "SELECT * FROM t LIMIT 10"


def test_write_and_non_select_are_rejected():
    assert _code("DROP TABLE t") == "write_statement"
    assert _code("UPDATE t SET a=1") == "write_statement"
    assert _code("show tables") == "not_select_start"
    assert _code("WITH x AS (SELECT 1) SELECT * FROM x") == "not_select_start"


def test_multiple_statements_are_rejected():
    assert _code("SELECT 1; SELECT 2") == "multiple_statements"


def test_locking_and_dangerous_functions_are_rejected():
    assert _code("SELECT * FROM t FOR UPDATE") == "unsafe_construct"
    assert _code("SELECT * FROM t LOCK IN SHARE MODE") == "unsafe_construct"
    assert _code("SELECT SLEEP(10)") == "dangerous_function"
    assert _code("SELECT LOAD_FILE('/etc/passwd')") == "dangerous_function"
    assert _code("SELECT BENCHMARK(1000000, MD5('x'))") == "dangerous_function"


def test_into_outfile_is_rejected_as_parse_error():
    assert _code("SELECT * FROM t INTO OUTFILE '/tmp/x'") == "parse_error"


def test_dangerous_word_inside_string_is_not_treated_as_write():
    assert validate_readonly_sql("SELECT 'drop' AS word FROM t") == "SELECT 'drop' AS word FROM t LIMIT 500"


def test_schema_and_table_allowlists():
    sql = "SELECT * FROM `share-order`.`orders`"
    assert validate_readonly_sql(sql, allowed_schemas={"share-order"}) == sql + " LIMIT 500"
    assert _code("SELECT * FROM other.t", allowed_schemas={"share-order"}) == "table_not_allowed"
    assert _code("SELECT * FROM `share-order`.`orders`", allowed_tables={"customers"}) == "table_not_allowed"
    assert validate_readonly_sql("SELECT * FROM orders", allowed_tables={"orders"}) == "SELECT * FROM orders LIMIT 500"


def test_unqualified_table_uses_default_schema_for_scope():
    assert _code("SELECT * FROM orders", allowed_schemas={"share-order"}) == "table_not_allowed"
    assert validate_readonly_sql(
        "SELECT * FROM orders", allowed_schemas={"share-order"}, default_schema="share-order",
    ) == "SELECT * FROM orders LIMIT 500"


def test_file_dialect_allows_only_df():
    sql = "SELECT 科目, 分数 FROM df"
    assert validate_readonly_sql(sql, dialect="duckdb", allowed_tables={"df"}) == sql + " LIMIT 500"
    with pytest.raises(SqlPolicyError) as exc:
        validate_readonly_sql("SELECT * FROM other", dialect="duckdb", allowed_tables={"df"})
    assert exc.value.code == "table_not_allowed"


def test_policy_message_keeps_tool_specific_wording():
    with pytest.raises(SqlPolicyError) as exc:
        validate_readonly_sql("DROP TABLE t")
    assert "仅允许 SELECT" in policy_message(exc.value, tool="mysql")
    assert "query_file 只支持 SELECT" in policy_message(exc.value, tool="file")
    with pytest.raises(SqlPolicyError) as exc:
        validate_readonly_sql("show tables")
    assert "必须以 SELECT 开头" in policy_message(exc.value)
