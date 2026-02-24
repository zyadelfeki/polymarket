"""
Institutional-grade portfolio risk engine.

Enforces at all times:
  - Max total exposure across all open positions (default: 30% of equity)
  - Max exposure per market category (default: 10% of equity)
  - Max exposure per single market (default: 5% of equity)
  - Max correlated (same-asset) exposure (default: 15%)
  - Hard cap of 2 simultaneous open positions per correlated asset

This module is intentionally STATELESS — it reads open position data
passed in by the caller rather than querying the DB itself.  This keeps
it pure/testable and decoupled from the async ledger.

Usage::

    risk = PortfolioRiskEngine(config)
    approved_size, reject_reason = await risk.check_and_size(
        market_id="1403734",
        market_question="Bitcoin Up or Down 4:30PM",
        kelly_size=Decimal("150.00"),
        equity=Decimal("10000.00"),
        open_positions=[{"market_id": ..., "cost": ..., "question": ...}, ...],
    )
    if reject_reason:
        logger.warning("order_blocked_portfolio_risk", reason=reject_reason)
        return
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Tuple


class PortfolioRiskEngine:
    """
    Category-aware risk engine.

    Parameters (all as fractions of equity, e.g. 0.30 = 30%):
      max_total_exposure_pct:    cap on sum of all open position costs
      max_category_exposure_pct: cap per named category (BTC, ETH, POLITICS, ...)
      max_single_market_pct:     cap per individual market_id
      max_correlated_pct:        cap when 2+ positions share the same asset
      max_same_asset_positions:  hard position count cap per correlated asset
    """

    # Keyword → category mapping used by categorize()
    _CATEGORY_KEYWORDS: Dict[str, List[str]] = {
        "BTC":      ["btc", "bitcoin"],
        "ETH":      ["eth", "ethereum"],
        "SOL":      ["sol", "solana"],
        "POLITICS": ["trump", "election", "president", "senate", "congress",
                     "democrat", "republican"],
        "MACRO":    ["fed", "rate", "inflation", "cpi", "fomc", "gdp", "rbi"],
        "SPORTS":   ["nba", "nfl", "mlb", "nhl", "soccer", "premier", "champions"],
        "CRYPTO":   ["crypto", "defi", "nft", "altcoin", "memecoin", "doge",
                     "matic", "avax", "ada"],
    }

    def __init__(self, config: Optional[Dict] = None) -> None:
        cfg = config or {}
        self.max_total_exposure_pct     = Decimal(str(cfg.get("max_total_exposure_pct",     0.30)))
        self.max_category_exposure_pct  = Decimal(str(cfg.get("max_category_exposure_pct",  0.10)))
        self.max_single_market_pct      = Decimal(str(cfg.get("max_single_market_pct",      0.05)))
        self.max_correlated_pct         = Decimal(str(cfg.get("max_correlated_pct",         0.15)))
        self.max_same_asset_positions   = int(cfg.get("max_same_asset_positions", 2))
        # $0.25 floor: below Polymarket's practical minimum order (~$1), so this
        # never silently vetoes a genuinely-sized trade.  Real protection comes
        # from the percentage caps above.  Callers can raise this via config.
        self._min_tradeable             = Decimal(str(cfg.get("min_tradeable_usdc",          0.25)))

    # ------------------------------------------------------------------

    def categorize(self, market_question: str) -> str:
        """Classify a market question into a risk category."""
        q = market_question.lower()
        for cat, keywords in self._CATEGORY_KEYWORDS.items():
            if any(kw in q for kw in keywords):
                return cat
        return "OTHER"

    def check_and_size(
        self,
        market_id: str,
        market_question: str,
        kelly_size: Decimal,
        equity: Decimal,
        open_positions: List[Dict],
    ) -> Tuple[Decimal, str]:
        """
        Synchronous check — returns (approved_size, reject_reason).

        ``reject_reason`` is empty string on approval.
        ``approved_size`` may be smaller than ``kelly_size`` when a cap is
        reached but remaining room > min_tradeable_usdc.

        open_positions rows expected keys:
          market_id   str
          cost        Decimal  (amount staked in USDC, not quantity in tokens)
          question    str      (market question used for categorization)
        """
        if equity <= Decimal("0"):
            return Decimal("0"), "non_positive_equity"

        category = self.categorize(market_question)

        # Compute exposures from currently open positions
        cat_exposure     = Decimal("0")
        market_exposure  = Decimal("0")
        total_exposure   = Decimal("0")
        same_asset_count = 0

        for pos in open_positions:
            cost     = _to_dec(pos.get("cost", 0))
            pos_mkt  = pos.get("market_id", "")
            pos_q    = pos.get("question", "")
            pos_cat  = self.categorize(pos_q)

            total_exposure += cost
            if pos_cat == category:
                cat_exposure += cost
                same_asset_count += 1
            if pos_mkt == market_id:
                market_exposure += cost

        # --- Hard position count cap (correlated assets) -------------------
        # e.g. 4 simultaneous BTC 15-min markets = 4 perfectly correlated bets
        if same_asset_count >= self.max_same_asset_positions and category != "OTHER":
            return Decimal("0"), (
                f"same_asset_position_cap:{category}"
                f" ({same_asset_count}>={self.max_same_asset_positions})"
            )

        # --- Per-market cap -------------------------------------------------
        max_single_abs = equity * self.max_single_market_pct
        if market_exposure + kelly_size > max_single_abs:
            remaining = max_single_abs - market_exposure
            if remaining < self._min_tradeable:
                return Decimal("0"), f"single_market_cap_exceeded:{market_id}"
            kelly_size = min(kelly_size, remaining)

        # --- Category cap ---------------------------------------------------
        max_cat_abs = equity * self.max_category_exposure_pct
        if cat_exposure + kelly_size > max_cat_abs:
            remaining = max_cat_abs - cat_exposure
            if remaining < self._min_tradeable:
                return Decimal("0"), f"category_cap_exceeded:{category}"
            kelly_size = min(kelly_size, remaining)

        # --- Total exposure cap ---------------------------------------------
        max_total_abs = equity * self.max_total_exposure_pct
        if total_exposure + kelly_size > max_total_abs:
            remaining = max_total_abs - total_exposure
            if remaining < self._min_tradeable:
                return Decimal("0"), "total_exposure_cap_exceeded"
            kelly_size = min(kelly_size, remaining)

        return kelly_size, ""


# ---------------------------------------------------------------------------
# DrawdownMonitor — rolling peak equity with auto-halt on drawdown
# ---------------------------------------------------------------------------

class DrawdownMonitor:
    """
    Monitors rolling drawdown from peak equity.

    Call ``update()`` before every strategy scan.  Returns False when
    drawdown has reached ``max_drawdown_pct`` — the caller must skip
    the scan entirely.

    Auto-resumes when equity recovers 50% of the drawdown from the halt
    point, reducing annoying thrash when equity fluctuates near the limit.
    """

    def __init__(self, max_drawdown_pct: float = 0.10) -> None:
        self.max_drawdown_pct = Decimal(str(max_drawdown_pct))
        self.peak_equity:    Optional[Decimal] = None
        self.trading_halted: bool = False

    def update(self, current_equity: Decimal, logger) -> bool:
        """
        Update peak and check drawdown.

        Returns True  → trading allowed.
        Returns False → trading halted (drawdown kill switch active).
        """
        if self.peak_equity is None or current_equity > self.peak_equity:
            self.peak_equity = current_equity

        drawdown = (self.peak_equity - current_equity) / self.peak_equity

        if drawdown >= self.max_drawdown_pct and not self.trading_halted:
            self.trading_halted = True
            logger.critical(
                "drawdown_kill_switch_triggered",
                peak_equity=str(self.peak_equity),
                current_equity=str(current_equity),
                drawdown_pct=str(round(float(drawdown), 4)),
                max_allowed=str(self.max_drawdown_pct),
            )

        if self.trading_halted and drawdown < self.max_drawdown_pct * Decimal("0.5"):
            self.trading_halted = False
            logger.warning(
                "drawdown_kill_switch_released",
                current_equity=str(current_equity),
                peak_equity=str(self.peak_equity),
                current_drawdown_pct=str(round(float(drawdown), 4)),
            )

        return not self.trading_halted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")
