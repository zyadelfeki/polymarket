"""
Polymarket dynamic taker fee calculator.

Fee formula (from Polymarket docs):
    fee = FEE_RATE * (p * (1 - p)) ** FEE_EXPONENT

where:
    FEE_RATE  = 0.25   (25% of the variance term)
    FEE_EXPONENT = 2   (squared — penalises mid-range prices more)

Fee peaks at p = 0.50 (~1.56%) and drops towards the extremes:
    p = 0.50  →  fee ≈ 1.56%
    p = 0.70  →  fee ≈ 1.10%
    p = 0.85  →  fee ≈ 0.41%
    p = 0.95  →  fee ≈ 0.06%
"""

from __future__ import annotations

from decimal import Decimal

FEE_RATE = Decimal("0.25")
FEE_EXPONENT = 2


def taker_fee_rate(price: Decimal) -> Decimal:
    """Return the taker fee as a fraction of trade value.

    Parameters
    ----------
    price : Decimal
        Market price (probability), e.g. Decimal("0.60").

    Returns
    -------
    Decimal
        Fee fraction, e.g. Decimal("0.0144") for price=0.60.
    """
    p = Decimal(str(price))
    return FEE_RATE * (p * (Decimal("1") - p)) ** FEE_EXPONENT


def net_edge(p_win: Decimal, market_price: Decimal) -> Decimal:
    """Return edge after taker fees.

    Parameters
    ----------
    p_win : Decimal
        Predicted win probability (possibly calibrated).
    market_price : Decimal
        Current market price (implied probability).

    Returns
    -------
    Decimal
        Gross edge minus the dynamic taker fee.  Only trade if this
        exceeds ``MIN_NET_EDGE``.
    """
    gross = Decimal(str(p_win)) - Decimal(str(market_price))
    fee = taker_fee_rate(market_price)
    return gross - fee
