"""Thread-safe async in-memory cache for LLM inference results.

TTL-based eviction and question hash validation ensure stale or wrong-context
entries are never served.  All operations are O(1) amortised.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

CACHE_TTL_SECONDS: float = 300.0


@dataclass
class CacheEntry:
    result: Any
    timestamp: float       # time.monotonic() at insertion
    question_hash: int     # hash(question) — guards against market-id reuse


class LLMCache:
    """Per-market LLM result cache.

    Keys are market_id strings.  A hit is valid only when:
      - the entry is younger than ``ttl`` seconds, AND
      - the stored question hash matches the caller's question.

    Designed to be shared across the event loop from a single async context;
    the asyncio.Lock serialises concurrent cache writers (the worker) and
    readers (the scanner).
    """

    def __init__(self, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._store: Dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, question: str) -> Optional[Any]:
        """Return cached result or None on miss / expiry / question mismatch."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.monotonic() - entry.timestamp > self._ttl:
                del self._store[key]
                return None
            if entry.question_hash != hash(question):
                del self._store[key]
                return None
            return entry.result

    async def set(self, key: str, question: str, result: Any) -> None:
        """Insert or overwrite a cache entry."""
        async with self._lock:
            self._store[key] = CacheEntry(
                result=result,
                timestamp=time.monotonic(),
                question_hash=hash(question),
            )

    async def size(self) -> int:
        """Number of entries currently held (including potentially expired ones)."""
        async with self._lock:
            return len(self._store)
