"""
LLM-based edge quality scorer.
Adds qualitative assessment of WHY Charlie's edge is or isn't reliable
for a specific market at a specific time.
Does NOT affect sizing. Output is logged and stored for feedback learning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ai.llm_client import llm_query

_EDGE_PROMPT_TEMPLATE = """\
[INST] You are a prediction market edge analyst. Answer with ONLY valid JSON.

Market: "{question}"
BTC current price: ${btc_price:.0f}
Market strike price: {strike}
Time to resolution: {minutes_to_expiry} minutes
Charlie model edge: {edge:.1%}
Market YES implied probability: {implied_prob:.1%}
Charlie confidence: {confidence:.1%}

Rate the QUALITY of this edge from 0.0 to 1.0. Consider:
- Is the strike close to current price? (closer = better quality edge)
- Is there enough time for the prediction to play out? (too short = noise, too long = decay)
- Is the implied probability far from 50%? (near 50% = genuine uncertainty, near 0/100 = nearly resolved)
- Does the edge size match the confidence level?

List any quality flags as short strings.

Respond with JSON only: {{"score": 0.0-1.0, "flags": ["flag1", "flag2"], "summary": "one sentence"}}
[/INST]"""

_PASSTHROUGH_SCORE = 0.5


@dataclass
class EdgeQuality:
    score: float                   # 0.0 (garbage) to 1.0 (high conviction)
    flags: List[str] = field(default_factory=list)  # e.g. ["very_short_timeframe", "strike_far_from_price"]
    summary: str = "llm_unavailable"
    source: str = "passthrough"    # "llm" or "passthrough"


def _passthrough() -> EdgeQuality:
    return EdgeQuality(score=_PASSTHROUGH_SCORE, flags=[], summary="llm_unavailable", source="passthrough")


async def score_edge_quality(
    question: str,
    btc_price: float,
    strike: float,
    minutes_to_expiry: float,
    edge: float,
    implied_prob: float,
    confidence: float,
) -> EdgeQuality:
    """
    Ask Phi-3 to rate the structural quality of Charlie's edge for this market.
    Returns a neutral passthrough (score=0.5) on any LLM failure.
    Never raises; never blocks a trade.
    """
    prompt = _EDGE_PROMPT_TEMPLATE.format(
        question=question,
        btc_price=btc_price,
        strike=strike,
        minutes_to_expiry=minutes_to_expiry,
        edge=edge,
        implied_prob=implied_prob,
        confidence=confidence,
    )

    raw = await llm_query(prompt, expect_json=True)

    if not isinstance(raw, dict):
        return _passthrough()

    try:
        score = float(raw.get("score", _PASSTHROUGH_SCORE))
        # Clamp to valid range
        score = max(0.0, min(1.0, score))
        flags = [str(f) for f in raw.get("flags", []) if isinstance(raw.get("flags", []), list)]
        summary = str(raw.get("summary", ""))
        return EdgeQuality(score=score, flags=flags, summary=summary, source="llm")
    except Exception:
        return _passthrough()
