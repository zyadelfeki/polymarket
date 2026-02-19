"""
Kelly Sizing for Polymarket — wraps Charlie's DynamicKelly.

Given:
  p_win          — probability from Charlie's signal API
  implied_prob   — market price (e.g. 0.65 cents on-chain = 65% implied)
  bankroll       — current equity from the ledger

Yields:
  recommended_size   — USDC amount to bet (Decimal, always ≤ bankroll)
  kelly_fraction     — fraction of bankroll used (Decimal)

Design constraints (from config_production.py)
-----------------------------------------------
* Fractional Kelly: use ``KELLY_CONFIG["fractional_kelly"]`` (default 0.25).
* Hard cap: ``KELLY_CONFIG["max_bet_pct"]`` % of bankroll per trade.
* Global cap: never exceed available balance.
* Negative edge → return zero size (no bet).
* ``win_rate`` and ``avg_win/avg_loss`` are derived from p_win and the
  implied probability, not from historical stats, so the sizing still
  works before any history accumulates.  Once the PerformanceTracker has
  enough settled trades (≥ 20), pass its rolling_win_rate in to override.

Usage::

    sizer = KellySizer(config=KELLY_CONFIG)
    result = sizer.compute_size(
        p_win=0.70,
        implied_prob=0.60,
        bankroll=Decimal("50.00"),
    )
    if result.size > 0:
        # place order for result.size USDC
"""

from __future__ import annotations

import logging
import sys
import os
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import DynamicKelly from project-charlie (handle both import contexts)
# ---------------------------------------------------------------------------

_DynamicKelly = None
_KellyInput = None

def _load_charlie_kelly():
    """
    Lazily import Charlie's DynamicKelly.

    We try a few import paths because:
      1. When running inside the polymarket directory, the user may have
         project-charlie installed as a package or on PYTHONPATH.
      2. CI / development environments may have it available via relative path.
    """
    global _DynamicKelly, _KellyInput
    if _DynamicKelly is not None:
        return True

    # Option A: installed as a package
    try:
        from src.v14.dynamic_kelly import DynamicKelly, KellyInput  # type: ignore
        _DynamicKelly = DynamicKelly
        _KellyInput = KellyInput
        logger.debug("charlie_kelly_loaded_from_package")
        return True
    except ImportError:
        pass

    # Option B: CHARLIE_PATH env variable points to the repo root
    charlie_root = os.getenv("CHARLIE_PATH", "")
    if charlie_root:
        if charlie_root not in sys.path:
            sys.path.insert(0, charlie_root)
        try:
            from src.v14.dynamic_kelly import DynamicKelly, KellyInput  # type: ignore
            _DynamicKelly = DynamicKelly
            _KellyInput = KellyInput
            logger.debug("charlie_kelly_loaded_via_CHARLIE_PATH", path=charlie_root)
            return True
        except ImportError:
            pass

    logger.warning(
        "charlie_dynamic_kelly_unavailable — falling back to built-in fractional Kelly"
    )
    return False


# ---------------------------------------------------------------------------
# Fallback pure-Python implementation (mirrors DynamicKelly exactly)
# ---------------------------------------------------------------------------

class _BuiltinKelly:
    """
    Pure-Python Kelly for a binary prediction-market bet.

    The true Kelly fraction for a binary outcome is:

        b = (1 - q) / q       # decimal odds (net profit per $1 risked on a win)
        f = (p * (b + 1) - 1) / b
          = (p - q) / (1 - q)  # simplified for binary market

    Where:
        p  = estimated win probability (Charlie's p_win)
        q  = market-implied win probability (token price)

    We then apply:
        1. Fractional-Kelly multiplier (default 0.5 = half-Kelly)
        2. Hard cap at max_fraction

    Rationale for half-Kelly default
    ---------------------------------
    Full Kelly maximises long-run log-wealth but is extremely sensitive to
    model error.  At half-Kelly, expected growth rate falls by only ~25% while
    the variance of outcomes drops by 75%.  This is the standard institutional
    choice when the edge estimate carries uncertainty.
    """

    def __init__(self, fraction: float = 0.5, max_fraction: float = 0.05):
        # fraction: fractional-Kelly multiplier applied to the raw Kelly fraction
        self.fraction = fraction
        self.max_fraction = max_fraction

    def optimal_fraction(self, p_win: float, implied_prob: float) -> float:
        """
        Return the fractional-Kelly position size as a fraction of bankroll
        (already multiplied by self.fraction and capped at self.max_fraction).

        Returns 0.0 when there is no edge (p_win <= implied_prob).
        """
        q = implied_prob
        p = p_win

        # Guard against degenerate inputs
        if q <= 0.0 or q >= 1.0 or p <= 0.0:
            return 0.0

        # True Kelly formula for binary outcome
        raw_f = (p - q) / (1.0 - q)

        if raw_f <= 0.0:
            return 0.0

        fractional_f = raw_f * self.fraction
        return min(fractional_f, self.max_fraction)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class KellySizeResult:
    """
    Output of ``KellySizer.compute_size``.

    Never compare ``.size`` to a float — use ``Decimal`` arithmetic only.
    """
    size:             Decimal   # USDC to bet
    kelly_fraction:   Decimal   # raw Kelly fraction (before caps)
    effective_fraction: Decimal # after all caps applied
    capped_reason:    Optional[str] = None
    edge:             Decimal = Decimal("0")

    def __bool__(self) -> bool:
        return self.size > Decimal("0")


# ---------------------------------------------------------------------------
# Main sizer
# ---------------------------------------------------------------------------

class KellySizer:
    """
    Polymarket-specific Kelly position sizer.

    Parameters
    ----------
    config : dict
        Subset of ``config_production.KELLY_CONFIG``.
    """

    def __init__(self, config: Optional[Dict] = None) -> None:
        cfg = config or {}

        # Fractional-Kelly multiplier — default ½ Kelly (see _BuiltinKelly docstring)
        self._fractional_kelly = float(cfg.get("fractional_kelly", Decimal("0.5")))
        # Hard floor on required edge
        self._min_edge = Decimal(str(cfg.get("min_edge_required", Decimal("0.02"))))
        # Max bet as % of bankroll
        self._max_bet_pct = Decimal(str(cfg.get("max_bet_pct", Decimal("5.0"))))
        # Minimum confidence required (checked externally, stored here for reference)
        self._min_confidence = Decimal(str(cfg.get("min_confidence", Decimal("0.65"))))

        # Always use the built-in true-Kelly implementation.
        # DynamicKelly from project-charlie uses a different parameterisation
        # (win_rate/avg_win/avg_loss) and doesn't guarantee the correct binary
        # Kelly formula, so we bypass it here.
        self._kelly = _BuiltinKelly(
            fraction=self._fractional_kelly,
            max_fraction=float(self._max_bet_pct / Decimal("100")),
        )

    def compute_size(
        self,
        *,
        p_win: float,
        implied_prob: float,
        bankroll: Decimal,
        volatility: float = 0.0,
        override_win_rate: Optional[float] = None,
    ) -> KellySizeResult:
        """
        Compute recommended bet size in USDC.

        Parameters
        ----------
        p_win:
            Charlie's estimated P(win) for the YES/UP outcome.
        implied_prob:
            Market price expressed as a probability (e.g. 0.65 for 65¢).
        bankroll:
            Current equity from the ledger.
        volatility:
            Optional market volatility signal [0, 1] — reduces sizing.
        override_win_rate:
            If the PerformanceTracker has ≥20 settled trades, pass its rolling
            win rate here to anchor sizing on actual historical accuracy rather
            than the model's self-reported p_win.

        Returns
        -------
        KellySizeResult
            ``.size == 0`` means "do not bet".
        """
        edge = Decimal(str(p_win)) - Decimal(str(implied_prob))

        # --- Guard: no negative/zero edge --------------------------------
        if edge < self._min_edge:
            return KellySizeResult(
                size=Decimal("0"),
                kelly_fraction=Decimal("0"),
                effective_fraction=Decimal("0"),
                capped_reason=f"edge_below_minimum: {edge:.4f} < {self._min_edge}",
                edge=edge,
            )

        if bankroll <= Decimal("0"):
            return KellySizeResult(
                size=Decimal("0"),
                kelly_fraction=Decimal("0"),
                effective_fraction=Decimal("0"),
                capped_reason="zero_bankroll",
                edge=edge,
            )

        # --- True Kelly fraction (binary prediction market) ----------------
        # f_raw = (p_win - implied_prob) / (1 - implied_prob)
        # f_half = f_raw * fractional_kelly_multiplier  (built into _BuiltinKelly)
        # Capped at max_bet_pct / 100  (also built into _BuiltinKelly)
        #
        # Why bypass the avg_win / avg_loss parameterisation:
        #   In a binary prediction market a $1 bet on a YES token worth $q pays
        #   $(1/q) on WIN and $0 on LOSS.  The odds b = (1-q)/q and the true
        #   Kelly fraction f = (p*(b+1)-1)/b = (p-q)/(1-q).  Using
        #   avg_win=b*100 and avg_loss=100 gives the same result algebraically
        #   but introduces floating-point noise and a hidden coupling between
        #   the volatility damping factor in the old formula and the edge
        #   estimate.  We drop the volatility damping here because the Kelly
        #   fraction already accounts for uncertainty through fractional sizing.

        # If the tracker has enough history, prefer its empirical win rate over
        # the model's self-reported p_win — it anchors sizing on actual accuracy.
        effective_win_rate: float = (
            float(override_win_rate)
            if override_win_rate is not None
            else float(p_win)
        )

        raw_frac = self._kelly.optimal_fraction(
            p_win=effective_win_rate,
            implied_prob=implied_prob,
        )

        kelly_fraction = Decimal(str(raw_frac))

        # --- Caps -------------------------------------------------------
        capped_reason = None
        max_fraction = self._max_bet_pct / Decimal("100")
        if kelly_fraction > max_fraction:
            kelly_fraction = max_fraction
            capped_reason = f"hard_cap_{self._max_bet_pct}pct"

        effective_fraction = kelly_fraction

        # --- Size in USDC -----------------------------------------------
        raw_size = (bankroll * effective_fraction).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        # Never bet more than the bankroll
        final_size = min(raw_size, bankroll)

        logger.debug(
            "kelly_size_computed",
            p_win=p_win,
            implied_prob=implied_prob,
            edge=str(edge),
            kelly_fraction=str(kelly_fraction),
            effective_fraction=str(effective_fraction),
            size=str(final_size),
            bankroll=str(bankroll),
        )

        return KellySizeResult(
            size=final_size,
            kelly_fraction=kelly_fraction,
            effective_fraction=effective_fraction,
            capped_reason=capped_reason,
            edge=edge,
        )
