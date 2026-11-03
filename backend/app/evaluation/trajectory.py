"""Task-specific predicates over actual tool observations, without a fixed path."""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope


def validate_trajectory(spec: dict, files: list[str]) -> None:
    if not isinstance(spec, dict) or not spec or spec.keys() - {
        "required_file_reads", "max_file_queries", "no_repeat_failed_sql", "require_query_timeout",
    }:
        raise ValueError("invalid trajectory specification")
    if "required_file_reads" in spec:
        required = spec["required_file_reads"]
        if (not isinstance(required, list) or not required
                or any(not isinstance(item, str) or item not in files for item in required)
                or len(required) != len(set(required))):
            raise ValueError("trajectory reads must be distinct fixture files")
    if "max_file_queries" in spec and (type(spec["max_file_queries"]) is not int or spec["max_file_queries"] < 0):
        raise ValueError("invalid trajectory query limit")
    for key in ("no_repeat_failed_sql", "require_query_timeout"):
        if key in spec and spec[key] is not True:
            raise ValueError(f"{key} must be true when specified")


def _sql_key(sql: str) -> tuple:
    try:
        statements = sqlglot.parse(sql, read="duckdb")
        return tuple(statement.sql(dialect="duckdb", comments=False) for statement in statements if statement)
    except SqlglotError:
        return (sql.strip().rstrip(";").strip(),)


def _uses_uploaded_relation(sql: str) -> bool:
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
        return any(isinstance(source, exp.Table) and source.name.casefold() == "df"
                   and not source.db and not source.catalog
                   for scope in traverse_scope(tree) for _, source in scope.selected_sources.values())
    except SqlglotError:
        return False


def trajectory_checks(spec: dict, calls, *, complete=True) -> list:
    """Yield (name, passed, known) with missing/incomplete observation fail-closed."""
    known = isinstance(calls, list) and all(
        isinstance(call, dict) and isinstance(call.get("tool"), str)
        and call.get("status") in ({"ok", "error", "timeout"} if complete else {"ok", "error", "timeout", "running"})
        for call in calls
    )
    queries = [c for c in calls if c["tool"] == "query_file"] if known else []
    known = known and all(
        isinstance(c.get("arguments"), dict)
        and isinstance(c["arguments"].get("file"), str)
        and isinstance(c["arguments"].get("sql"), str)
        and (c["status"] != "ok" or isinstance(c.get("table"), dict)
             or (not complete and c.get("table_observed") is True))
        for c in queries
    )
    if not known:
        return [(f"trajectory:{key}", False, False) for key in spec]
    files = {c["arguments"]["file"] for c in queries if c["status"] == "ok"
             and _uses_uploaded_relation((c.get("table") or {}).get("executed_sql") or c["arguments"]["sql"])}
    failed = set()
    repeated = False
    for call in queries:
        args = call["arguments"]
        key = args["file"], _sql_key(args["sql"])
        repeated = repeated or key in failed
        if call["status"] == "error":
            failed.add(key)
    values = {
        "required_file_reads": lambda required: set(required) <= files,
        "max_file_queries": lambda limit: len(queries) <= limit,
        "no_repeat_failed_sql": lambda _: not repeated,
        "require_query_timeout": lambda _: any(c["status"] == "timeout" for c in queries),
    }
    checks = []
    for key, value in spec.items():
        passed = values[key](value)
        # Some facts can be established from a prefix, but it cannot prove that
        # a missing read never happened or that no additional retry would occur.
        positive_witness = key in {"required_file_reads", "require_query_timeout"}
        checks.append((f"trajectory:{key}", passed, complete or passed == positive_witness))
    return checks
