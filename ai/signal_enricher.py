"""
LLM-based coherence check and anomaly veto.
Called after Charlie approves a market, before order submission.

Veto threshold: confidence >= 0.75 AND verdict is negative.
LLM failure = pass-through (never blocks a Charlie-approved trade).
"""
from __future__ import annotations

from dataclasses import dataclass

from ai.llm_client import llm_query
from ai.market_parser import MarketContext

_COHERENCE_PROMPT_TEMPLATE = """\
[INST] You are a crypto trading analyst. Answer with ONLY valid JSON.

Current market state:
- BTC price: ${btc_price:.0f}
- RSI-14: {rsi:.1f} ({rsi_label})
- MACD: {macd_val:.2f} ({macd_label})
- Recommended trade: {side} on "{question}"
- Model win probability: {p_win:.1%}

Is this trade coherent with current BTC momentum? Consider: does current momentum support the predicted direction? Are there obvious contradictions (e.g. RSI overbought but betting further UP)?

Respond with JSON only: {{"coherent": true or false, "reason": "max 15 words", "confidence": 0.0-1.0}}
[/INST]"""

_ANOMALY_PROMPT_TEMPLATE = """\
[INST] You are a prediction market analyst. Answer with ONLY valid JSON.

BTC is currently at ${btc_price:.0f}.
Market question: "{question}"
Market YES price (implied probability): {market_price:.1%}

Is this market an obvious trap or non-tradeable situation?
Examples of traps: market already resolved, strike price far from current price (>20% away), question is nonsensical, implied probability suggests market is nearly certain already.

Respond with JSON only: {{"is_trap": true or false, "reason": "max 15 words", "confidence": 0.0-1.0}}
[/INST]"""

# Veto fires only when LLM is confident the trade is bad
_VETO_CONFIDENCE_THRESHOLD = 0.75


@dataclass
class CoherenceResult:
    coherent: bool
    confidence: float
    reason: str
    vetoed: bool    # True only if coherent=False AND confidence >= 0.75
    source: str     # "llm" or "passthrough"


_PASSTHROUGH = CoherenceResult(
    coherent=True,
    confidence=0.0,
    reason="llm_unavailable",
    vetoed=False,
    source="passthrough",
)


async def check_coherence(
    *,
    context: MarketContext,
    btc_price: float,
    rsi: float,
    macd_val: float,
    charlie_side: str,
    p_win: float,
    market_price: float,
) -> CoherenceResult:
    """
    Ask Phi-3 whether Charlie's recommended trade is coherent with current momentum.
    Returns PASSTHROUGH (vetoed=False) on any LLM failure.
    """
    rsi_label = "overbought" if rsi > 70 else ("oversold" if rsi < 30 else "neutral")
    macd_label = "bullish" if macd_val > 0 else "bearish"

    prompt = _COHERENCE_PROMPT_TEMPLATE.format(
        btc_price=btc_price,
        rsi=rsi,
        rsi_label=rsi_label,
        macd_val=macd_val,
        macd_label=macd_label,
        side=charlie_side,
        question=context.question,
        p_win=p_win,
    )

    result = await llm_query(prompt, expect_json=True)
    if not isinstance(result, dict):
        return _PASSTHROUGH

    try:
        coherent = bool(result.get("coherent", True))
        confidence = float(result.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        reason = str(result.get("reason", ""))[:200]
        vetoed = (not coherent) and (confidence >= _VETO_CONFIDENCE_THRESHOLD)
        return CoherenceResult(
            coherent=coherent,
            confidence=confidence,
            reason=reason,
            vetoed=vetoed,
            source="llm",
        )
    except (TypeError, ValueError):
        return _PASSTHROUGH


async def anomaly_veto(question: str, btc_price: float, *, market_price: float = 0.5) -> bool:
    """
    Returns True if Phi-3 detects an obvious trap/anomaly with high confidence.
    Returns False on any LLM failure — never blocks on uncertainty.
    """
    prompt = _ANOMALY_PROMPT_TEMPLATE.format(
        btc_price=btc_price,
        question=question,
        market_price=market_price,
    )

    result = await llm_query(prompt, expect_json=True)
    if not isinstance(result, dict):
        return False

    try:
        is_trap = bool(result.get("is_trap", False))
        confidence = float(result.get("confidence", 0.0))
        return is_trap and confidence >= _VETO_CONFIDENCE_THRESHOLD
    except (TypeError, ValueError):
        return False
