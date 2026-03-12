"""
Unit tests for ai/regime_guard.py

Covers: cache TTL enforcement, concurrency lock, confidence threshold logic,
LLM failure passthrough, and cache-hit debug logging.
"""
import asyncio
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest

import ai.regime_guard as rg
from ai.regime_guard import get_regime_verdict, _PASSTHROUGH, REGIME_CACHE_TTL_SECONDS


def _clear_cache():
    rg._regime_cache.clear()


BASE_ARGS = dict(
    btc_price=82000.0,
    rsi=55.0,
    price_change_1h=0.5,
    atr_pct=1.2,
    open_positions=1,
)

GOOD_LLM_RESPONSE = {
    "safe_to_trade": True,
    "regime_label": "STABLE",
    "confidence": 0.90,
    "reason": "steady momentum",
}


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear module-level cache before every test."""
    _clear_cache()
    yield
    _clear_cache()


@pytest.mark.asyncio
async def test_cache_hit_prevents_second_llm_call():
    """Second call within TTL must return cached verdict without calling llm_query again."""
    with patch("ai.regime_guard.llm_query", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = GOOD_LLM_RESPONSE

        v1 = await get_regime_verdict(**BASE_ARGS)
        v2 = await get_regime_verdict(**BASE_ARGS)

    assert mock_llm.call_count == 1, "llm_query called more than once within TTL!"
    assert v1.regime_label == v2.regime_label == "STABLE"


@pytest.mark.asyncio
async def test_cache_expires_after_ttl():
    """After TTL expiry, llm_query must be called again."""
    with patch("ai.regime_guard.llm_query", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = GOOD_LLM_RESPONSE
        await get_regime_verdict(**BASE_ARGS)

        # Manually expire the cache
        rg._regime_cache["expires_at"] = 0.0

        await get_regime_verdict(**BASE_ARGS)

    assert mock_llm.call_count == 2


@pytest.mark.asyncio
async def test_confidence_below_threshold_passes_through():
    """
    LLM says safe_to_trade=False with confidence=0.65.
    0.65 < _SUPPRESS_CONFIDENCE_THRESHOLD (0.70) -> safe_to_trade flipped to True.
    """
    with patch("ai.regime_guard.llm_query", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = {
            "safe_to_trade": False,
            "regime_label": "RISK_OFF",
            "confidence": 0.65,
            "reason": "uncertain signal",
        }
        verdict = await get_regime_verdict(**BASE_ARGS)

    assert verdict.safe_to_trade is True, "Low-confidence veto should be ignored!"
    assert verdict.regime_label == "RISK_OFF"


@pytest.mark.asyncio
async def test_confidence_above_threshold_suppresses():
    """
    LLM says safe_to_trade=False with confidence=0.85.
    0.85 >= 0.70 -> safe_to_trade stays False.
    """
    with patch("ai.regime_guard.llm_query", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = {
            "safe_to_trade": False,
            "regime_label": "FLASH_CRASH",
            "confidence": 0.85,
            "reason": "rapid sell-off detected",
        }
        verdict = await get_regime_verdict(**BASE_ARGS)

    assert verdict.safe_to_trade is False, "High-confidence veto should suppress trading!"
    assert verdict.regime_label == "FLASH_CRASH"


@pytest.mark.asyncio
async def test_llm_failure_returns_passthrough():
    """When llm_query returns a non-dict, PASSTHROUGH (safe_to_trade=True) is returned."""
    with patch("ai.regime_guard.llm_query", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "not a dict"
        verdict = await get_regime_verdict(**BASE_ARGS)

    assert verdict.safe_to_trade is True
    assert verdict.regime_label == "UNKNOWN"
    assert verdict.source == "passthrough"


@pytest.mark.asyncio
async def test_cache_hit_emits_debug_log(caplog):
    """Cache-hit path must emit a DEBUG log containing 'regime_cache_hit'."""
    with patch("ai.regime_guard.llm_query", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = GOOD_LLM_RESPONSE
        await get_regime_verdict(**BASE_ARGS)  # populates cache

        with caplog.at_level(logging.DEBUG, logger="ai.regime_guard"):
            await get_regime_verdict(**BASE_ARGS)  # cache hit

    assert any("regime_cache_hit" in r.message for r in caplog.records), \
        "Expected 'regime_cache_hit' in DEBUG logs on cache hit"


@pytest.mark.asyncio
async def test_ttl_is_60_seconds():
    """REGIME_CACHE_TTL_SECONDS must be 60, not 120 (the old dangerous value)."""
    assert REGIME_CACHE_TTL_SECONDS == 60, (
        f"TTL is {REGIME_CACHE_TTL_SECONDS}s — expected 60s. "
        "A 120s TTL means 8 stale scan cycles during a flash crash!"
    )
