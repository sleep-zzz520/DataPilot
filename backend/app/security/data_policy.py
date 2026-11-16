"""Deterministic masking and sensitive-table access checks.

The catalog is the source of truth for explicitly marked sensitive columns and
tables. A small name-pattern fallback protects uploaded files and schemas that
have not been annotated yet. Masking happens before tool output reaches the
LLM, so raw values never enter the model context.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

import pandas as pd
import sqlglot
from sqlglot import exp

from app.semantic.catalog import SemanticCatalog, load_semantic_catalog

MASK = "***"

_COLUMN_RE = re.compile(
    r"(^|_)(phone|mobile|tel|telephone|email|mail|id_?card|identity_?card|passport|"
    r"bank_?card|card_?no|password|passwd|secret|api_?key|token|address|addr|"
    r"salary|wage|income|birthday|birth_?date)(_|$)"
    r"|姓名|手机号|手机号码|电话|邮箱|身份证|护照|银行卡|住址|地址|工资|薪资|收入|生日|密码|密钥",
    re.I,
)
_AGGREGATE_ALIAS = re.compile(
    r"(^|_)(sum|avg|average|count|min|max|total|mean|median)(_|$)"
    r"|平均|合计|总计|最大|最小|总数|求和|均值|计数",
    re.I,
)
_EMAIL = re.compile(r"^([^@\s])[^@\s]*(@.+)$")
_DIGITS = re.compile(r"\D+")


def _split_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _normalize_name(value) -> str:
    return str(value or "").strip().strip("`").lower()


def mask_value(value) -> str:
    """Mask one sensitive value while keeping a stable, non-reversible shape."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value)
    email = _EMAIL.match(text)
    if email:
        return f"{email.group(1)}***{email.group(2)}"
    digits = _DIGITS.sub("", text)
    if 7 <= len(digits) <= 15:
        return f"{digits[:3]}****{digits[-4:]}"
    if len(text) >= 8:
        return f"{text[:2]}{MASK}{text[-2:]}"
    return MASK


def _is_aggregate_alias(name: str) -> bool:
    return bool(_AGGREGATE_ALIAS.search(str(name or "")))


@dataclass(frozen=True)
class DataPolicy:
    sensitive_tables: frozenset
    sensitive_columns: frozenset
    mask_patterns: bool = True

    def is_sensitive_column(self, name: str) -> bool:
        normalized = _normalize_name(name)
        if not normalized:
            return False
        return normalized in self.sensitive_columns or (
            self.mask_patterns and bool(_COLUMN_RE.search(normalized))
        )

    def mask_dataframe(self, df: pd.DataFrame,
                       extra_columns: Optional[Iterable[str]] = None
                       ) -> tuple[pd.DataFrame, list[str]]:
        extras = {_normalize_name(item) for item in (extra_columns or []) if _normalize_name(item)}
        selected = [
            column for column in df.columns
            if _normalize_name(column) in extras or self.is_sensitive_column(str(column))
        ]
        if not selected:
            return df, []
        masked = df.copy()
        masked_columns = []
        for column in selected:
            series = masked[column]
            if pd.api.types.is_numeric_dtype(series):
                # A single row can contain raw PII (e.g. SELECT salary LIMIT 1).
                # Row count alone cannot establish that a value is aggregated.
                if _is_aggregate_alias(str(column)):
                    continue
                masked[column] = MASK
            else:
                masked[column] = series.map(mask_value)
            masked_columns.append(str(column))
        return masked, masked_columns

    def sensitive_tables_in_sql(self, sql: str, *, dialect: str = "mysql",
                                default_schema: Optional[str] = None) -> set[str]:
        if not self.sensitive_tables:
            return set()
        try:
            statement = sqlglot.parse_one(sql, read=dialect)
        except Exception:  # noqa: BLE001 - policy checks never crash the tool
            return set()
        cte_names = {str(cte.alias_or_name).lower() for cte in statement.find_all(exp.CTE)}
        hits = set()
        for table in statement.find_all(exp.Table):
            name = _normalize_name(table.name)
            if not name or name in cte_names:
                continue
            schema = _normalize_name(table.db or default_schema)
            qualified = f"{schema}.{name}" if schema else name
            if qualified in self.sensitive_tables or name in self.sensitive_tables:
                hits.add(qualified)
        return hits

    def sensitive_columns_for_sql(self, sql: str, *, dialect: str = "mysql",
                                  source_columns: Optional[Iterable[str]] = None) -> set[str]:
        """Resolve aliases of sensitive source columns so renamed results stay masked.

        Aggregates are intentionally excluded: ``AVG(salary)`` is a usable metric,
        while a row-level ``salary AS pay`` remains sensitive.
        """
        try:
            statement = sqlglot.parse_one(sql, read=dialect)
        except Exception:  # noqa: BLE001
            return set()
        sensitive = set(self.sensitive_columns)
        sensitive.update(_normalize_name(item) for item in (source_columns or []) if _normalize_name(item))
        extras: set[str] = set()
        for select in statement.find_all(exp.Select):
            for projection in select.expressions:
                if projection.find(exp.AggFunc) is not None:
                    continue
                if isinstance(projection, exp.Star) or projection.find(exp.Star) is not None:
                    extras.update(sensitive)
                    continue
                columns = {_normalize_name(column.name) for column in projection.find_all(exp.Column)}
                if (columns & sensitive) or (
                    self.mask_patterns and any(_COLUMN_RE.search(column) for column in columns)
                ):
                    alias = _normalize_name(projection.alias_or_name)
                    if alias:
                        extras.add(alias)
        return extras


def load_data_policy(catalog: Optional[SemanticCatalog] = None) -> DataPolicy:
    catalog = catalog or load_semantic_catalog()
    tables: set[str] = set()
    columns: set[str] = set()
    for table in catalog.tables:
        qualified = _normalize_name(table.qualified_name)
        bare = _normalize_name(table.table_name)
        if table.sensitive:
            if qualified:
                tables.add(qualified)
            if bare:
                tables.add(bare)
        for column in table.columns:
            if column.sensitive and column.name:
                columns.add(_normalize_name(column.name))
    for name in _split_env("SENSITIVE_TABLES"):
        tables.add(name)
    for name in _split_env("SENSITIVE_COLUMNS"):
        columns.add(name)
    return DataPolicy(frozenset(tables), frozenset(columns))
