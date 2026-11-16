"""Application-owned semantic catalog.

The catalog is intentionally a local, owner-edited file. It is never loaded from
LLM output or user input, and it is only a context source; trusted metric
contracts remain the authority for executable formulas.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SEMANTIC_CATALOG = _BACKEND_DIR / "config" / "semantic_catalog.json"


class SemanticColumn(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(min_length=1)
    type: Optional[str] = None
    description: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    sample_values: List[str] = Field(default_factory=list)
    key: Optional[str] = None
    sensitive: bool = False
    always_include: bool = False

    @field_validator("synonyms", "sample_values", mode="before")
    @classmethod
    def _coerce_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return list(value)

    @field_validator("name", "type", "description", "key", mode="before")
    @classmethod
    def _coerce_text(cls, value):
        return None if value is None else str(value)


class SemanticTable(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(min_length=1)
    schema_name: Optional[str] = Field(default=None, alias="schema")
    description: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)
    domain: Optional[str] = None
    owner: Optional[str] = None
    freshness: Optional[str] = None
    sensitive: bool = False
    columns: List[SemanticColumn] = Field(default_factory=list)
    unique_keys: List[List[str]] = Field(default_factory=list)
    always_include: bool = False

    @field_validator("synonyms", mode="before")
    @classmethod
    def _coerce_synonyms(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return list(value)

    @field_validator("columns", mode="before")
    @classmethod
    def _coerce_columns(cls, value):
        if value is None:
            return []
        result = []
        for item in value:
            result.append({"name": item} if isinstance(item, str) else item)
        return result

    @field_validator("unique_keys", mode="before")
    @classmethod
    def _coerce_unique_keys(cls, value):
        if value is None:
            return []
        result = []
        for item in value:
            result.append([item] if isinstance(item, str) else list(item))
        return result

    @property
    def qualified_name(self) -> str:
        if "." in self.name:
            return self.name
        if self.schema_name:
            return f"{self.schema_name}.{self.name}"
        return self.name

    @property
    def table_name(self) -> str:
        return self.name.rsplit(".", 1)[-1]

    @property
    def effective_schema(self) -> Optional[str]:
        if self.schema_name:
            return self.schema_name
        if "." in self.name:
            return self.name.rsplit(".", 1)[0]
        return None

    @property
    def column_names(self) -> List[str]:
        return [column.name for column in self.columns]


class SemanticMetric(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Optional[str] = None
    name: str = Field(min_length=1)
    synonyms: List[str] = Field(default_factory=list)
    description: Optional[str] = None
    formula: Optional[str] = None
    source: Optional[str] = None
    required_columns: List[str] = Field(default_factory=list)
    filters: List[str] = Field(default_factory=list)
    grain: Optional[str] = None
    unit: Optional[str] = None
    contract_id: Optional[str] = None

    @field_validator("synonyms", "required_columns", "filters", mode="before")
    @classmethod
    def _coerce_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return list(value)


class SemanticJoin(BaseModel):
    model_config = ConfigDict(extra="ignore")

    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    left_on: List[str] = Field(default_factory=list)
    right_on: List[str] = Field(default_factory=list)
    cardinality: str = "many_to_one"
    description: Optional[str] = None

    @field_validator("left_on", "right_on", mode="before")
    @classmethod
    def _coerce_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return list(value)


class VerifiedQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    tables: List[str] = Field(default_factory=list)
    description: Optional[str] = None

    @field_validator("tables", mode="before")
    @classmethod
    def _coerce_tables(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return list(value)


class SemanticCatalog(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str = "1"
    tables: List[SemanticTable] = Field(default_factory=list)
    metrics: List[SemanticMetric] = Field(default_factory=list)
    joins: List[SemanticJoin] = Field(default_factory=list)
    verified_queries: List[VerifiedQuery] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.tables or self.metrics or self.joins or self.verified_queries)

    def table_map(self) -> Dict[str, SemanticTable]:
        result: Dict[str, SemanticTable] = {}
        for table in self.tables:
            result[table.qualified_name.lower()] = table
            if table.effective_schema:
                result[f"{table.effective_schema}.{table.table_name}".lower()] = table
            result.setdefault(table.table_name.lower(), table)
        return result

    def find_table(self, name: str) -> Optional[SemanticTable]:
        if not name:
            return None
        return self.table_map().get(str(name).strip("`").lower())

    def metrics_for_tables(self, tables: Iterable[str]) -> List[SemanticMetric]:
        wanted = {str(name).lower() for name in tables}
        result = []
        for metric in self.metrics:
            source = (metric.source or "").lower()
            if source and (source in wanted or source.split(".")[-1] in wanted):
                result.append(metric)
        return result


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_semantic_catalog(path: Optional[Path] = None) -> SemanticCatalog:
    """Load the owner-edited catalog; malformed or missing files fail open to empty."""
    target = Path(path) if path is not None else DEFAULT_SEMANTIC_CATALOG
    if not target.exists():
        return SemanticCatalog()
    try:
        return SemanticCatalog.model_validate(_read_json(target))
    except Exception as exc:  # noqa: BLE001 - catalog is optional context, never block chat
        logger.warning("semantic catalog ignored: %s", exc)
        return SemanticCatalog()
