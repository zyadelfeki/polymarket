"""
LLM-based macro regime guard.
Runs once per scan cycle. If LLM detects an adverse macro regime,
it can suppress all bets for that cycle regardless of Charlie signal.
Result is cached for REGIME_CACHE_TTL_SECONDS. LLM failure = PASS.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict

from ai.llm_client import llm_query

REGIME_CACHE_TTL_SECONDS = 120  # re-evaluate every 2 minutes

_REGIME_PROMPT_TEMPLATE = """\
[INST] You are a macro market analyst. Answer with ONLY valid JSON.

BTC market snapshot:
- Current price: ${btc_price:.0f}
- RSI-14: {rsi:.1f}
- Price change last hour: {price_change_1h:+.2f}%
- Volatility (ATR%): {atr_pct:.2f}%
- Active open positions: {open_positions}

Classify the current macro trading regime. Consider: Is this a stable trending market? A risk-off event (rapid selling, extreme RSI, high ATR)? A flash crash? A news-driven spike?

Classify into one of: STABLE, TRENDING_UP, TRENDING_DOWN, RISK_OFF, FLASH_CRASH, HIGH_VOL_UNCERTAIN

Should a momentum prediction bot trade in this regime?

Respond with JSON only: {{"safe_to_trade": true or false, "regime_label": "...", "confidence": 0.0-1.0, "reason": "max 20 words"}}
[/INST]"""

# Veto fires only when LLM is highly confident the market is unsafe
_SUPPRESS_CONFIDENCE_THRESHOLD = 0.70

# Module-level cache: simple dict avoids a global singleton class
_regime_cache: Dict[str, Any] = {}


@dataclass
class RegimeVerdict:
    safe_to_trade: bool       # False = suppress all bets this cycle
    regime_label: str         # e.g. "RISK_OFF", "STABLE", "FLASH_CRASH", "UNKNOWN"
    confidence: float         # 0.0–1.0
    reason: str               # max 20 words
    source: str               # "llm" or "passthrough"


_PASSTHROUGH = RegimeVerdict(
    safe_to_trade=True,
    regime_label="UNKNOWN",
    confidence=0.0,
    reason="llm_unavailable",
    source="passthrough",
)


async def get_regime_verdict(
    btc_price: float,
    rsi: float,
    price_change_1h: float,
    atr_pct: float,
    open_positions: int,
) -> RegimeVerdict:
    """
    Query Phi-3 for the current macro regime. Returns a cached result if fresh.
    On any LLM failure, returns PASSTHROUGH (safe_to_trade=True) — never blocks.
    """
    now = time.monotonic()

    cached_entry = _regime_cache.get("verdict")
    expires_at = _regime_cache.get("expires_at", 0.0)
    if cached_entry is not None and now < expires_at:
        return cached_entry

    prompt = _REGIME_PROMPT_TEMPLATE.format(
        btc_price=btc_price,
        rsi=rsi,
        price_change_1h=price_change_1h,
        atr_pct=atr_pct,
        open_positions=open_positions,
    )

    raw = await llm_query(prompt, expect_json=True)

    if not isinstance(raw, dict):
        _regime_cache["verdict"] = _PASSTHROUGH
        _regime_cache["expires_at"] = now + REGIME_CACHE_TTL_SECONDS
        return _PASSTHROUGH

    try:
        llm_safe = bool(raw.get("safe_to_trade", True))
        label = str(raw.get("regime_label", "UNKNOWN"))
        confidence = float(raw.get("confidence", 0.0))
        reason = str(raw.get("reason", ""))

        # Only suppress when confidence is high enough to trust the veto
        if not llm_safe and confidence < _SUPPRESS_CONFIDENCE_THRESHOLD:
            llm_safe = True

        verdict = RegimeVerdict(
            safe_to_trade=llm_safe,
            regime_label=label,
            confidence=confidence,
            reason=reason,
            source="llm",
        )
    except Exception:
        verdict = _PASSTHROUGH

    _regime_cache["verdict"] = verdict
    _regime_cache["expires_at"] = now + REGIME_CACHE_TTL_SECONDS
    return verdict
