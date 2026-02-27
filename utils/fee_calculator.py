"""
Polymarket dynamic taker fee calculator.

Standard markets use a variance-based formula (from Polymarket docs):
    fee = FEE_RATE * (p * (1 - p)) ** FEE_EXPONENT

where:
    FEE_RATE  = 0.25   (25% of the variance term)
    FEE_EXPONENT = 2   (squared — penalises mid-range prices more)

Fee peaks at p = 0.50 (~1.56%) and drops towards the extremes:
    p = 0.50  →  fee ≈ 1.56%
    p = 0.70  →  fee ≈ 1.10%
    p = 0.85  →  fee ≈ 0.41%
    p = 0.95  →  fee ≈ 0.06%

Exception — Crypto Direction Markets (BTC/ETH/SOL 15-min up-or-down):
    Polymarket introduced a flat 3.15% taker fee on these markets as an
    anti-arb countermeasure.  Any market whose question contains BTC, ETH,
    SOL, or direction keywords ('up or down', '15m', 'direction') is charged
    the higher flat rate instead of the variance formula.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

FEE_RATE = Decimal("0.25")
FEE_EXPONENT = 2

# Standard cap (variance formula peaks at ~1.56% at p=0.50)
STANDARD_TAKER_FEE_MAX = Decimal("0.02")

# BTC/ETH/SOL 15-min direction market flat taker fee (anti-arb countermeasure)
CRYPTO_DIRECTION_TAKER_FEE = Decimal("0.0315")

# Keywords that identify crypto direction markets priced at the higher rate
CRYPTO_DIRECTION_KEYWORDS = (
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
    "up or down", "direction", "15m", "15min",
)


def is_crypto_direction_market(
    market: Optional[dict] = None,
    question: Optional[str] = None,
) -> bool:
    """Return True if this is a BTC/ETH/SOL 15-min direction market.

    Accepts either a full market dict (uses 'question' or 'title' keys) or an
    explicit question string.  Passing both uses the explicit string.
    """
    if question is None and market is not None:
        question = str(market.get("question") or market.get("title") or "")
    if not question:
        return False
    q = question.lower()
    return any(kw in q for kw in CRYPTO_DIRECTION_KEYWORDS)


def taker_fee_rate(
    price: Decimal,
    market: Optional[dict] = None,
    question: Optional[str] = None,
) -> Decimal:
    """Return the taker fee as a fraction of trade value.

    Parameters
    ----------
    price : Decimal
        Market price (probability), e.g. Decimal("0.60").
    market : dict, optional
        Full market dict — used to detect crypto direction markets for the
        higher 3.15% flat fee.
    question : str, optional
        Market question text — alternative to passing the full dict.

    Returns
    -------
    Decimal
        Fee fraction.  Crypto direction markets: 0.0315 flat.
        All others: variance formula (peaks ~1.56% at p=0.50).
    """
    if is_crypto_direction_market(market=market, question=question):
        return CRYPTO_DIRECTION_TAKER_FEE
    p = Decimal(str(price))
    return FEE_RATE * (p * (Decimal("1") - p)) ** FEE_EXPONENT


def net_edge(
    p_win: Decimal,
    market_price: Decimal,
    market: Optional[dict] = None,
    question: Optional[str] = None,
) -> Decimal:
    """Return edge after taker fees.

    Parameters
    ----------
    p_win : Decimal
        Predicted win probability (possibly calibrated).
    market_price : Decimal
        Current market price (implied probability).
    market : dict, optional
        Full market dict — forwarded to taker_fee_rate for crypto detection.
    question : str, optional
        Market question text — alternative to passing the full dict.

    Returns
    -------
    Decimal
        Gross edge minus the dynamic taker fee.  Only trade if this
        exceeds ``MIN_NET_EDGE``.
    """
    gross = Decimal(str(p_win)) - Decimal(str(market_price))
    fee = taker_fee_rate(market_price, market=market, question=question)
    return gross - fee
