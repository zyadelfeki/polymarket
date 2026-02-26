"""
Yes/No Sum Arbitrage Scanner.

On Polymarket, YES + NO shares for the same binary market must sum to $1.00.
If market makers mis-price both sides:
    YES ask = $0.47, NO ask = $0.47 -> buy both = $0.94, guaranteed $1.00 payout
    = 6% risk-free return.

This scanner detects such opportunities after accounting for dynamic taker fees.
"""

from __future__ import annotations

from decimal import Decimal
import structlog

from utils.fee_calculator import taker_fee_rate

logger = structlog.get_logger(__name__)

MIN_ARB_PCT = Decimal("0.005")  # minimum 0.5% net after fees


def scan_yes_no_arb(markets: list[dict], clob_client) -> list[dict]:
    """
    For each market, fetch YES ask and NO ask from the CLOB.
    If YES_ask + NO_ask < 1.00 - fees, return the arb opportunity.

    Parameters
    ----------
    markets : list[dict]
        Active markets from _market_discovery_probe (each has 'tokens').
    clob_client :
        Polymarket CLOB client with get_last_trade_price(token_id).

    Returns
    -------
    list[dict]
        Sorted by net_arb_pct descending.  Each dict:
          {market_id, yes_token, no_token, yes_ask, no_ask,
           total_cost, gross_arb_pct, net_arb_pct}
    """
    opportunities: list[dict] = []

    for market in markets:
        try:
            tokens = market.get("tokens") or []
            if len(tokens) < 2:
                continue

            condition_id = (
                market.get("condition_id")
                or market.get("market_id")
                or market.get("id")
                or ""
            )
            yes_token = tokens[0].get("token_id", "")
            no_token = tokens[1].get("token_id", "")

            if not yes_token or not no_token:
                continue

            # Fetch last trade price for each leg
            try:
                yes_ask = Decimal(str(clob_client.get_last_trade_price(yes_token)))
                no_ask = Decimal(str(clob_client.get_last_trade_price(no_token)))
            except Exception:
                continue

            if yes_ask <= 0 or no_ask <= 0:
                continue

            total_cost = yes_ask + no_ask

            # Dynamic fees on each leg
            fee_yes = taker_fee_rate(yes_ask)
            fee_no = taker_fee_rate(no_ask)
            total_fees = fee_yes + fee_no

            gross_arb = Decimal("1.0") - total_cost
            net_arb = gross_arb - total_fees

            if net_arb >= MIN_ARB_PCT:
                opp = {
                    "market_id": condition_id,
                    "yes_token": yes_token,
                    "no_token": no_token,
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                    "total_cost": total_cost,
                    "gross_arb_pct": float(gross_arb),
                    "net_arb_pct": float(net_arb),
                    "total_fees": float(total_fees),
                }
                opportunities.append(opp)
                logger.info(
                    "yes_no_arb_found",
                    market_id=condition_id,
                    net_arb_pct=float(net_arb),
                    gross_arb_pct=float(gross_arb),
                    yes_ask=float(yes_ask),
                    no_ask=float(no_ask),
                    total_fees=float(total_fees),
                )

        except Exception as exc:
            logger.warning(
                "arb_scan_error",
                error=str(exc),
                market_id=market.get("condition_id", "unknown"),
            )

    return sorted(opportunities, key=lambda x: x["net_arb_pct"], reverse=True)
