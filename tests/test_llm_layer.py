"""
Tests for the LLM intelligence layer: market_parser and signal_enricher.
All tests mock ai.llm_client.llm_query — no real ollama calls made.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ai.market_parser import MarketContext, parse_market_question, is_btc_market
from ai.signal_enricher import CoherenceResult, check_coherence, anomaly_veto


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(question: str = "Will BTC reach $85,000?", asset: str = "BTC") -> MarketContext:
    return MarketContext(
        question=question,
        asset=asset,
        direction_yes="UP",
        strike=85000.0,
        timeframe_minutes=1440,
        parse_confidence=0.9,
        parse_source="llm",
    )


# ---------------------------------------------------------------------------
# Task 5 — test_market_parser_regex_fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_parser_regex_fallback():
    """When LLM returns None, regex fallback must produce a valid MarketContext."""
    with patch("ai.market_parser.llm_query", new=AsyncMock(return_value=None)):
        ctx = await parse_market_question("Will BTC reach $85,000 by end of day?")

    assert isinstance(ctx, MarketContext)
    assert ctx.parse_source == "regex"
    assert ctx.asset == "BTC"
    assert ctx.strike == 85000.0
    assert ctx.direction_yes == "UP"
    assert ctx.timeframe_minutes == 1440       # "end of day" → 1440
    assert 0.0 <= ctx.parse_confidence <= 1.0


# ---------------------------------------------------------------------------
# Task 5 — test_coherence_passthrough_on_failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_coherence_passthrough_on_failure():
    """When LLM returns None, check_coherence must return vetoed=False and source='passthrough'."""
    ctx = _make_context()
    with patch("ai.signal_enricher.llm_query", new=AsyncMock(return_value=None)):
        result = await check_coherence(
            context=ctx,
            btc_price=84000.0,
            rsi=55.0,
            macd_val=120.0,
            charlie_side="YES",
            p_win=0.62,
            market_price=0.42,
        )

    assert isinstance(result, CoherenceResult)
    assert result.vetoed is False
    assert result.source == "passthrough"


# ---------------------------------------------------------------------------
# Task 5 — test_anomaly_passthrough_on_failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_anomaly_passthrough_on_failure():
    """When LLM returns None, anomaly_veto must return False (never block on unavailability)."""
    with patch("ai.signal_enricher.llm_query", new=AsyncMock(return_value=None)):
        result = await anomaly_veto("Will BTC exceed $85,000?", btc_price=84000.0)

    assert result is False


# ---------------------------------------------------------------------------
# Task 5 — test_is_btc_market
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_is_btc_market_true():
    """BTC question must be identified as a BTC market."""
    with patch("ai.market_parser.llm_query", new=AsyncMock(return_value=None)):
        ctx = await parse_market_question("Will BTC reach $80,000?")
    assert is_btc_market(ctx) is True


@pytest.mark.asyncio
async def test_is_btc_market_false_for_eth():
    """ETH question must NOT be identified as a BTC market."""
    with patch("ai.market_parser.llm_query", new=AsyncMock(return_value=None)):
        ctx = await parse_market_question("Will ETH hit $3000?")
    assert is_btc_market(ctx) is False


# ---------------------------------------------------------------------------
# LLMCache tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_cache_miss_returns_none():
    """Fresh cache must return None for any key."""
    from ai.llm_cache import LLMCache
    cache = LLMCache()
    assert await cache.get("mkt_001", "Will BTC reach $85k?") is None


@pytest.mark.asyncio
async def test_llm_cache_set_and_get():
    """A value stored with set() must be retrievable with the same key/question."""
    from ai.llm_cache import LLMCache
    cache = LLMCache()
    await cache.set("mkt_001", "Will BTC reach $85k?", (False, None))
    result = await cache.get("mkt_001", "Will BTC reach $85k?")
    assert result == (False, None)


@pytest.mark.asyncio
async def test_llm_cache_ttl_expiry():
    """Entries older than ttl must be evicted and return None."""
    import asyncio as _asyncio
    from ai.llm_cache import LLMCache
    cache = LLMCache(ttl=0.01)
    await cache.set("mkt_002", "Will BTC reach $90k?", (True, None))
    await _asyncio.sleep(0.05)
    assert await cache.get("mkt_002", "Will BTC reach $90k?") is None


@pytest.mark.asyncio
async def test_llm_worker_enqueue_does_not_block():
    """Enqueueing past maxsize must not raise; queue stays within maxsize."""
    from ai.llm_worker import LLMWorker
    worker = LLMWorker(maxsize=500)
    for i in range(600):
        worker.enqueue([{
            "market_id":    f"mkt_{i}",
            "question":     f"Will BTC hit ${80000 + i}?",
            "market_price": 0.5,
            "btc_price":    84000.0,
            "rsi":          50.0,
            "macd":         0.0,
            "charlie_side": "YES",
            "p_win":        0.6,
        }])
    assert worker._queue.qsize() <= 500
