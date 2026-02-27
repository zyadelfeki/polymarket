"""
Market-tag lookup utility.

Reads tags produced by ``scripts/tag_market_questions.py`` from
``data/market_tags.db`` (SQLite) and provides a fast, cached lookup for
whether a market matches any entry in ``MARKET_TAG_BLOCKLIST``.

Design
------
- Single-process cache with a 5-minute TTL so fresh tags are picked up
  without a restart.
- Fail-open: if the tag DB is absent or a lookup fails, the market is
  NOT blocked (returns False).  Missing tags = current behaviour.
- Never called on the latency-critical order-placement path from the OFI
  or Charlie routes.  Called only once per market per scan in
  _execute_opportunity, after the static blocked-market guard.

Public API
----------
    from utils.market_tags import is_market_blocked_by_tags

    if is_market_blocked_by_tags(market_id, market_question, blocklist):
        # skip this market
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

_REPO_ROOT  = Path(__file__).resolve().parent.parent
_TAGS_DB    = _REPO_ROOT / "data" / "market_tags.db"

_cache: Dict[str, Optional[dict]] = {}
_cache_ts: float = 0.0
_cache_lock = threading.Lock()
_CACHE_TTL: float = 300.0   # 5 minutes


def is_market_blocked_by_tags(
    market_id: str,
    market_question: str,
    blocklist: Optional[List[dict]] = None,
) -> bool:
    """
    Return True if the market's tags match any entry in ``blocklist``.

    Parameters
    ----------
    market_id:       Polymarket condition_id / numeric id.
    market_question: Question text (used for fallback keyword matching when
                     no tag record exists).
    blocklist:       List of tag-condition dicts from config_production.
                     Each dict is an AND-match: all keys must match.
                     If None or empty, always returns False.

    Fail-open: returns False on any exception.
    """
    if not blocklist:
        return False
    try:
        tags = _get_tags(market_id)
        if tags is None:
            return False  # no tag record → don't block
        for entry in blocklist:
            if _matches_entry(tags, entry):
                return True
        return False
    except Exception:
        return False


def get_tags(market_id: str) -> Optional[dict]:
    """Public wrapper around the cached tag lookup."""
    try:
        return _get_tags(market_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_tags(market_id: str) -> Optional[dict]:
    """
    Lookup tags for ``market_id`` from local cache, loading from DB if stale.
    Returns None when no tag row exists.
    """
    with _cache_lock:
        now_ts = time.monotonic()
        if (now_ts - _cache_ts) > _CACHE_TTL:
            _refresh_cache()

        return _cache.get(str(market_id))


def _refresh_cache() -> None:
    """
    Reload all tag rows from the SQLite database into _cache.
    Called under _cache_lock.  Any DB error is silently suppressed so
    a missing/corrupt tag DB never blocks the trading loop.
    """
    global _cache_ts
    if not _TAGS_DB.exists():
        _cache_ts = time.monotonic()
        return
    try:
        conn = sqlite3.connect(str(_TAGS_DB), timeout=2.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT market_id, asset, event_type, horizon, outcome_type, "
            "       asymmetry_flag, info_edge_needed "
            "FROM market_tags"
        ).fetchall()
        conn.close()
        _cache.clear()
        for row in rows:
            _cache[str(row["market_id"])] = dict(row)
        _cache_ts = time.monotonic()
    except Exception:
        _cache_ts = time.monotonic()  # suppress repeated reload attempts for TTL
        return


def _matches_entry(tags: dict, entry: dict) -> bool:
    """Return True if ALL keys in ``entry`` match the corresponding tag value."""
    for k, required_val in entry.items():
        actual_val = tags.get(k)
        if actual_val is None:
            return False
        if str(actual_val).lower() != str(required_val).lower():
            return False
    return True
