"""Short-lived, process-local cache for schema metadata.

The cache is deliberately small and dependency-free. It reduces repeated
INFORMATION_SCHEMA round trips inside a request and across nearby requests.
A TTL keeps schema changes visible without requiring a restart.
"""
from __future__ import annotations

import time
from threading import RLock
from typing import Dict, List, Optional, Tuple

from app.db.schema import get_schema_text, get_table_schema_text, get_tables_meta, list_tables


def _engine_key(engine) -> str:
    url = getattr(engine, "url", None)
    if url is not None:
        try:
            return url.render_as_string(hide_password=True)
        except Exception:  # noqa: BLE001 - some test doubles expose a partial URL
            return str(url)
    return f"engine:{id(engine)}"


class SchemaCache:
    def __init__(self, ttl_seconds: float = 300.0):
        self.ttl_seconds = ttl_seconds
        self._lock = RLock()
        self._tables: Dict[str, Tuple[float, list]] = {}
        self._columns: Dict[str, Tuple[float, dict]] = {}
        self._table_text: Dict[str, Tuple[float, str]] = {}
        self._schema_text: Dict[str, Tuple[float, str]] = {}

    def _fresh(self, bucket: dict, key: str):
        if self.ttl_seconds <= 0:
            return None
        with self._lock:
            item = bucket.get(key)
            if item is None:
                return None
            expires_at, value = item
            if time.monotonic() >= expires_at:
                bucket.pop(key, None)
                return None
            return value

    def _store(self, bucket: dict, key: str, value) -> None:
        if self.ttl_seconds <= 0:
            return
        with self._lock:
            bucket[key] = (time.monotonic() + self.ttl_seconds, value)

    def get_tables(self, engine, schema: Optional[str] = None, prefix: str = "share-") -> List[dict]:
        key = f"{_engine_key(engine)}|{schema or '*'}|{prefix}"
        cached = self._fresh(self._tables, key)
        if cached is not None:
            return cached
        tables = list_tables(engine, schema, prefix)
        self._store(self._tables, key, tables)
        return tables

    def get_columns(self, engine, schema: str, tables: List[str]) -> Dict[str, dict]:
        wanted = sorted(str(table) for table in dict.fromkeys(tables) if table)
        if not schema or not wanted:
            return {}
        key = f"{_engine_key(engine)}|{schema}|{','.join(wanted)}"
        cached = self._fresh(self._columns, key)
        if cached is not None:
            return cached
        meta = get_tables_meta(engine, schema, wanted)
        self._store(self._columns, key, meta)
        return meta

    def get_table_schema_text(self, engine, schema: str, tables: List[str]) -> str:
        wanted = sorted(str(table) for table in dict.fromkeys(tables) if table)
        if not schema or not wanted:
            return "(未指定表)"
        key = f"{_engine_key(engine)}|{schema}|{','.join(wanted)}"
        cached = self._fresh(self._table_text, key)
        if cached is not None:
            return cached
        text = get_table_schema_text(engine, schema, wanted)
        self._store(self._table_text, key, text)
        return text

    def get_schema_text(self, engine, schema: Optional[str] = None, prefix: str = "share-") -> str:
        key = f"{_engine_key(engine)}|{schema or '*'}|{prefix}"
        cached = self._fresh(self._schema_text, key)
        if cached is not None:
            return cached
        text = get_schema_text(engine, schema, prefix)
        self._store(self._schema_text, key, text)
        return text

    def clear(self) -> None:
        with self._lock:
            self._tables.clear()
            self._columns.clear()
            self._table_text.clear()
            self._schema_text.clear()


_CACHE = SchemaCache()


def get_schema_cache() -> SchemaCache:
    return _CACHE


def clear_schema_cache() -> None:
    _CACHE.clear()
