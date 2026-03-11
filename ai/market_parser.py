"""
Parses Polymarket question text into structured trading context.
Primary: Phi-3 Mini via llm_client. Fallback: regex heuristics.

Regex fallback is intentionally low-confidence (0.4) — it exists only to ensure
downstream code always receives a valid MarketContext regardless of LLM availability.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ai.llm_client import llm_query

_LLM_PROMPT_TEMPLATE = """\
[INST] Extract trading metadata from this prediction market question.
Return ONLY valid JSON with these exact keys: asset, direction_yes, strike, timeframe_minutes, parse_confidence.

Rules:
- asset: ticker symbol (BTC, ETH, SOL) or OTHER
- direction_yes: UP if YES means price goes up/above/over, DOWN if YES means price goes down/below/under, AMBIGUOUS otherwise
- strike: the specific price number mentioned, or null
- timeframe_minutes: resolution window in minutes (15 for "15 minutes", 1440 for "by end of day"), or null
- parse_confidence: float 0.0-1.0, how certain you are of this parse

Question: "{question}"
[/INST]"""


@dataclass
class MarketContext:
    question: str
    asset: str              # "BTC", "ETH", "SOL", "OTHER"
    direction_yes: str      # "UP", "DOWN", "AMBIGUOUS"
    strike: Optional[float]
    timeframe_minutes: Optional[int]
    parse_confidence: float  # 0.0–1.0
    parse_source: str        # "llm" or "regex"


async def parse_market_question(question: str) -> MarketContext:
    """Parse market question — LLM first, regex fallback on any failure."""
    llm_result = await llm_query(
        _LLM_PROMPT_TEMPLATE.format(question=question),
        expect_json=True,
    )
    if isinstance(llm_result, dict):
        try:
            return _build_from_llm(question, llm_result)
        except (KeyError, ValueError, TypeError):
            pass  # malformed LLM output — fall through to regex
    return _build_from_regex(question)


def _build_from_llm(question: str, data: dict) -> MarketContext:
    asset = str(data.get("asset", "OTHER")).upper().strip()
    if asset not in ("BTC", "ETH", "SOL", "OTHER"):
        asset = "OTHER"

    direction = str(data.get("direction_yes", "AMBIGUOUS")).upper().strip()
    if direction not in ("UP", "DOWN", "AMBIGUOUS"):
        direction = "AMBIGUOUS"

    raw_strike = data.get("strike")
    strike: Optional[float] = float(raw_strike) if raw_strike is not None else None

    raw_tf = data.get("timeframe_minutes")
    timeframe: Optional[int] = int(raw_tf) if raw_tf is not None else None

    confidence = float(data.get("parse_confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    return MarketContext(
        question=question,
        asset=asset,
        direction_yes=direction,
        strike=strike,
        timeframe_minutes=timeframe,
        parse_confidence=confidence,
        parse_source="llm",
    )


def _build_from_regex(question: str) -> MarketContext:
    """Regex heuristics — always succeeds, always low confidence."""
    q_lower = question.lower()

    # Asset detection
    if re.search(r"\bbtc\b|bitcoin", q_lower):
        asset = "BTC"
    elif re.search(r"\beth\b|ethereum", q_lower):
        asset = "ETH"
    elif re.search(r"\bsol\b|solana", q_lower):
        asset = "SOL"
    else:
        asset = "OTHER"

    # Strike price — first number > 1000
    strike: Optional[float] = None
    for m in re.finditer(r"\$?([\d,]+(?:\.\d+)?)", question):
        try:
            candidate = float(m.group(1).replace(",", ""))
            if candidate > 1000:
                strike = candidate
                break
        except ValueError:
            continue

    # Direction
    if re.search(r"\babove\b|\bover\b|\bexceed\b|\breach\b", q_lower):
        direction_yes = "UP"
    elif re.search(r"\bbelow\b|\bunder\b|\bdrop\b", q_lower):
        direction_yes = "DOWN"
    else:
        direction_yes = "AMBIGUOUS"

    # Timeframe
    timeframe: Optional[int] = None
    m_min = re.search(r"(\d+)\s*min", q_lower)
    if m_min:
        timeframe = int(m_min.group(1))
    elif "hour" in q_lower:
        timeframe = 60
    elif "day" in q_lower or "end of" in q_lower:
        timeframe = 1440

    return MarketContext(
        question=question,
        asset=asset,
        direction_yes=direction_yes,
        strike=strike,
        timeframe_minutes=timeframe,
        parse_confidence=0.4,  # regex is inherently imprecise
        parse_source="regex",
    )


def is_btc_market(ctx: MarketContext) -> bool:
    return ctx.asset == "BTC"
