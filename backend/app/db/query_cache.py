"""Bounded in-process TTL/LRU cache for read-only query results.

Keys include the caller's data scope and a SQL hash, so users and databases do
not share rows. Only successful deterministic SELECT results are cached; errors,
timeouts, and volatile SQL bypass the cache.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Optional


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


DEFAULT_TTL_SECONDS = _env_int("QUERY_CACHE_TTL_SECONDS", 30)
DEFAULT_MAX_ENTRIES = _env_int("QUERY_CACHE_MAX_ENTRIES", 64)
DEFAULT_MAX_ENTRY_CHARS = _env_int("QUERY_CACHE_MAX_ENTRY_CHARS", 200_000)

_VOLATILE_SQL = re.compile(
    r"\b(now|current_timestamp|current_date|current_time|sysdate|rand|random|uuid)\s*\(",
    re.I,
)


def cacheable_sql(sql: str) -> bool:
    text = str(sql or "")
    return bool(text.strip()) and not _VOLATILE_SQL.search(text)


class QueryResultCache:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_entries: int = DEFAULT_MAX_ENTRIES,
                 max_entry_chars: int = DEFAULT_MAX_ENTRY_CHARS):
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.max_entry_chars = max(1, int(max_entry_chars))
        self._data: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._sets = 0

    @staticmethod
    def make_key(namespace: str, sql: str, source: str = "") -> str:
        payload = f"{namespace}\x00{source}\x00{sql}".encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()

    def get(self, key: str) -> Optional[str]:
        if self.ttl_seconds <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None
            value, expires_at = item
            if now >= expires_at:
                self._data.pop(key, None)
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: str) -> bool:
        if self.ttl_seconds <= 0 or not value or len(value) > self.max_entry_chars:
            return False
        with self._lock:
            self._data[key] = (value, time.monotonic() + self.ttl_seconds)
            self._data.move_to_end(key)
            self._sets += 1
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
        return True

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {"entries": len(self._data), "ttl_seconds": self.ttl_seconds,
                    "max_entries": self.max_entries, "hits": self._hits,
                    "misses": self._misses, "sets": self._sets,
                    "hit_rate": round(self._hits / total, 4) if total else 0.0}


_cache: Optional[QueryResultCache] = None
_cache_lock = threading.Lock()


def get_query_cache() -> QueryResultCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = QueryResultCache()
    return _cache
