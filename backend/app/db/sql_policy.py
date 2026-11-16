"""AST-based read-only SQL policy.

This is the application-level boundary, independent of database grants. It
rejects writes, multi-statement payloads, dangerous functions and locking or
file-writing constructs before SQL reaches the driver.
"""
from __future__ import annotations

from typing import Iterable, Optional, Set

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


class SqlPolicyError(ValueError):
    def __init__(self, code: str, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or ""


_WRITE_NODE_NAMES = (
    "Insert", "Update", "Delete", "Create", "Drop", "Alter", "TruncateTable",
    "Command", "Transaction", "Commit", "Rollback", "Grant", "Revoke", "Merge",
    "Copy",
)
_WRITE_NODES = tuple(
    value for value in (getattr(exp, name, None) for name in _WRITE_NODE_NAMES)
    if isinstance(value, type)
)
_DANGEROUS_FUNCTIONS = {
    "SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK", "RELEASE_LOCK", "RELEASE_ALL_LOCKS",
    "MASTER_POS_WAIT", "WAIT_FOR_EXECUTED_GTID_SET", "SYS_EXEC", "SYS_EVAL",
}


def _normalized_set(values: Optional[Iterable[str]]) -> Set[str]:
    return {str(value).strip().strip("`").lower() for value in (values or []) if str(value).strip()}


def validate_readonly_sql(
    sql: str,
    *,
    dialect: str = "mysql",
    default_limit: Optional[int] = 500,
    allowed_schemas: Optional[Iterable[str]] = None,
    allowed_tables: Optional[Iterable[str]] = None,
    default_schema: Optional[str] = None,
) -> str:
    """Validate and normalize a read-only query, adding an outer LIMIT if absent."""
    text = (sql or "").strip()
    if not text:
        raise SqlPolicyError("parse_error", "SQL 为空")
    try:
        statements = sqlglot.parse(text, read=dialect)
    except (SqlglotError, ValueError) as exc:
        raise SqlPolicyError("parse_error", f"SQL 解析失败：{exc}", str(exc))
    if len(statements) != 1:
        raise SqlPolicyError("multiple_statements", "只允许单条 SQL，不要使用分号")
    root = statements[0]
    if root is None:
        raise SqlPolicyError("parse_error", "SQL 解析为空")
    if isinstance(root, _WRITE_NODES):
        raise SqlPolicyError("write_statement", "仅允许 SELECT，检测到写操作")
    if not isinstance(root, exp.Select):
        raise SqlPolicyError("not_select_start", "SQL 必须以 SELECT 开头")
    if root.args.get("with_") is not None:
        raise SqlPolicyError("not_select_start", "SQL 必须以 SELECT 开头")
    if root.args.get("into") is not None or root.find(exp.Into) is not None:
        raise SqlPolicyError("unsafe_construct", "禁止 SELECT INTO")
    if root.find(exp.Lock) is not None:
        raise SqlPolicyError("unsafe_construct", "禁止 SELECT FOR UPDATE / LOCK IN SHARE MODE")

    for func in root.find_all(exp.Func):
        name = str(getattr(func, "name", "") or "").upper()
        if name in _DANGEROUS_FUNCTIONS:
            raise SqlPolicyError("dangerous_function", f"禁止使用函数 {name}")

    schemas = _normalized_set(allowed_schemas)
    tables = _normalized_set(allowed_tables)
    bare_tables = {name.rsplit(".", 1)[-1] for name in tables}
    cte_names = {str(cte.alias_or_name).lower() for cte in root.find_all(exp.CTE)}
    for table in root.find_all(exp.Table):
        name = str(table.name or "").strip("`")
        schema = str(table.db or default_schema or "").strip("`")
        if name.lower() in cte_names:
            continue
        if schemas:
            if not schema:
                raise SqlPolicyError("table_not_allowed", f"表 {name} 未限定库名，不在本次授权范围内")
            if schema.lower() not in schemas:
                raise SqlPolicyError("table_not_allowed", f"库 {schema} 不在本次授权范围内")
        if tables:
            qualified = f"{schema}.{name}".lower() if schema else name.lower()
            if qualified not in tables and name.lower() not in bare_tables:
                raise SqlPolicyError("table_not_allowed", f"表 {qualified} 不在本次授权范围内")

    if default_limit is not None and default_limit > 0 and root.args.get("limit") is None:
        # Preserve the caller's SQL text for audit/evidence equality; only append
        # the outer LIMIT instead of regenerating the whole statement.
        return text.rstrip().rstrip(";").rstrip() + f" LIMIT {int(default_limit)}"
    return text


def policy_message(exc: SqlPolicyError, *, tool: str = "sql") -> str:
    if exc.code == "not_select_start":
        return "错误：SQL 必须以 SELECT 开头。"
    if exc.code == "write_statement":
        return ("错误：query_file 只支持 SELECT 查询。"
                if tool == "file" else "错误：仅允许 SELECT，检测到写操作关键字。")
    if exc.code == "multiple_statements":
        return ("错误：只允许单条 SQL，不要使用分号。"
                if tool == "file" else "错误：只允许单条 SQL。")
    if exc.code == "parse_error":
        return f"错误：SQL 解析失败：{exc.detail or exc.message}。请检查语法后重试。"
    if exc.code == "table_not_allowed":
        return f"错误：{exc.message}。"
    return f"错误：{exc.message}。"
