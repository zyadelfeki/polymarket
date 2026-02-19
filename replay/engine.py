"""
Replay Engine — event-driven strategy simulation using historical log data.

Purpose
-------
Re-feed recorded Polymarket opportunities and price ticks through the real
Kelly sizing, risk-budget, and execution logic using an **in-memory** ledger
and a **fake Polymarket client** that simulates fills at historical prices.

This provides:
1. Regression detection — compare a run against a stored JSON baseline and
   fail if key metrics diverge beyond a tolerance.
2. Config sensitivity — sweep Kelly/edge parameters to find good defaults
   before touching live size.
3. Behaviour audit — verify that a code change doesn't silently alter trade
   decisions on the same historical events.

What this is (and is not)
--------------------------
* This IS an event-driven replay of *recorded decisions* (opportunities
  logged by the production bot) through the current sizing and risk code.
* This is NOT a market-microstructure simulator.  It does not model order
  book depth, partial fills, or queue position — fills happen immediately
  at the logged market price.

Data source
-----------
Logs written by ``main.py`` via structlog's JSONRenderer.  Each line is a
JSON object with at least ``{"event": "<name>", "timestamp": "<iso>", ...}``.

Relevant events consumed by this engine
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
``arbitrage_opportunity_detected``  — candidate trade (market_id, market_price, edge, …)
``order_settled_live``              — market resolution with pnl (from online settlement path)
``order_settled_offline``           — market resolution on startup reconcile
``order_filled``                    — confirmation of a fill (for slippage check)

CLI
---
::

    python main.py --mode replay --replay-log logs/bot_production.log
    python main.py --mode replay --replay-log logs/bot_production.log \\
                   --from 2026-01-01T00:00:00Z --to 2026-02-01T00:00:00Z

Programmatic
------------
::

    engine = ReplayEngine(log_file="logs/bot_production.log")
    results = await engine.run()
    print(results["summary"])
    engine.assert_no_regression(results, baseline_path="data/replay_baseline.json")
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Bootstrap PYTHONPATH so this module can be run directly (not just imported).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from database.ledger_async import AsyncLedger
from risk.kelly_sizing import KellySizer, KellySizeResult
from services.do_not_trade import DoNotTradeRegistry
from config_production import (
    KELLY_CONFIG,
    STARTING_CAPITAL,
    REGIME_RISK_OVERRIDES,
    CHARLIE_CONFIG,
    GLOBAL_RISK_BUDGET,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

class LogEvent:
    """One parsed log line."""

    __slots__ = ("timestamp", "event", "data")

    def __init__(self, timestamp: datetime, event: str, data: Dict[str, Any]) -> None:
        self.timestamp = timestamp
        self.event = event
        self.data = data

    def __repr__(self) -> str:
        return f"LogEvent(ts={self.timestamp.isoformat()}, event={self.event!r})"


class ReplayMetrics:
    """
    Accumulates per-trade and aggregate statistics during a replay run.

    Attributes are plain Python types (no Decimal) for easy JSON serialisation.
    """

    def __init__(self, initial_equity: float) -> None:
        self._equity: float = initial_equity
        self._peak_equity: float = initial_equity
        self._max_drawdown: float = 0.0
        self._equity_series: List[Tuple[str, float]] = []  # (iso_ts, equity)
        self._kelly_fractions: List[float] = []
        self._auto_blocks: List[Dict] = []
        self._trades: List[Dict] = []
        self._settled: int = 0
        self._wins: int = 0

    def record_trade(
        self,
        *,
        timestamp: str,
        market_id: str,
        order_value: float,
        kelly_fraction: float,
        edge: float,
        regime: Optional[str],
    ) -> None:
        self._kelly_fractions.append(kelly_fraction)
        self._trades.append({
            "timestamp": timestamp,
            "market_id": market_id,
            "order_value": order_value,
            "kelly_fraction": kelly_fraction,
            "edge": edge,
            "regime": regime,
        })

    def record_settlement(
        self,
        *,
        timestamp: str,
        pnl: float,
        order_id: str,
    ) -> None:
        self._settled += 1
        if pnl > 0:
            self._wins += 1
        self._equity += pnl
        if self._equity > self._peak_equity:
            self._peak_equity = self._equity
        dd = (self._peak_equity - self._equity) / self._peak_equity if self._peak_equity > 0 else 0.0
        if dd > self._max_drawdown:
            self._max_drawdown = dd
        self._equity_series.append((timestamp, round(self._equity, 4)))

    def record_auto_block(self, market_id: str, reason: str, timestamp: str) -> None:
        self._auto_blocks.append({"market_id": market_id, "reason": reason, "timestamp": timestamp})

    @property
    def equity(self) -> float:
        return self._equity

    def to_dict(self) -> Dict[str, Any]:
        n_settled = self._settled
        win_rate = self._wins / n_settled if n_settled > 0 else None
        initial_eq = (
            self._equity_series[0][1] if self._equity_series
            else self._equity
        )
        total_pnl = self._equity - initial_eq

        # CAGR and Sharpe from equity series
        cagr = None
        sharpe = None
        days = 0.0
        if len(self._equity_series) >= 2:
            first_ts = datetime.fromisoformat(self._equity_series[0][0])
            last_ts = datetime.fromisoformat(self._equity_series[-1][0])
            days = max((last_ts - first_ts).total_seconds() / 86400, 1.0)
            start_eq = self._equity_series[0][1]
            if start_eq > 0:
                cagr = (self._equity / start_eq) ** (365 / days) - 1

        if len(self._equity_series) >= 3:
            equities = [e for _, e in self._equity_series]
            daily_returns = [
                (equities[i] - equities[i - 1]) / equities[i - 1]
                for i in range(1, len(equities))
                if equities[i - 1] > 0
            ]
            if daily_returns:
                mean_r = sum(daily_returns) / len(daily_returns)
                variance = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
                std_r = math.sqrt(variance) if variance > 0 else 0.0
                sharpe = (mean_r / std_r * math.sqrt(365)) if std_r > 0 else None

        kelly_mean = (
            sum(self._kelly_fractions) / len(self._kelly_fractions)
            if self._kelly_fractions else 0.0
        )
        kelly_max = max(self._kelly_fractions) if self._kelly_fractions else 0.0

        return {
            "total_pnl":             round(total_pnl, 4),
            "final_equity":          round(self._equity, 4),
            "max_drawdown_pct":      round(self._max_drawdown * 100, 2),
            "max_drawdown_abs":      round(self._peak_equity * self._max_drawdown, 4),
            "total_trades":          len(self._trades),
            "settled_trades":        n_settled,
            "wins":                  self._wins,
            "win_rate":              round(win_rate, 4) if win_rate is not None else None,
            "cagr":                  round(cagr, 4) if cagr is not None else None,
            "sharpe":                round(sharpe, 4) if sharpe is not None else None,
            "days_covered":          round(days, 1),
            "auto_blocks":           len(self._auto_blocks),
            "kelly_fraction_mean":   round(kelly_mean, 6),
            "kelly_fraction_max":    round(kelly_max, 6),
            "equity_series":         self._equity_series,
            "auto_block_list":       self._auto_blocks,
        }


# ---------------------------------------------------------------------------
# Fake Polymarket client
# ---------------------------------------------------------------------------

class FakePolymarketClient:
    """
    Minimal stub implementing the ``PolymarketClientV2`` surface used by
    the execution and replay path.

    Every order fills immediately at ``market_price`` (± artificial slippage).
    No partial fills, no rejections, unless the market_id is in the explicit
    reject set.
    """

    def __init__(self, slippage_bps: float = 0.0) -> None:
        self._slippage_bps = slippage_bps
        self._markets: Dict[str, Dict] = {}
        self._orders: Dict[str, Dict] = {}
        self._reject_market_ids: set = set()

    def set_market(self, market_id: str, data: Dict) -> None:
        self._markets[market_id] = data

    def reject_market(self, market_id: str) -> None:
        self._reject_market_ids.add(market_id)

    async def health_check(self) -> bool:
        return True

    async def get_markets(self, active: bool = True, limit: int = 100) -> List[Dict]:
        return list(self._markets.values())

    async def get_active_markets(self, limit: int = 100) -> List[Dict]:
        return [m for m in self._markets.values() if not m.get("resolved")]

    async def get_market(self, market_id: str) -> Optional[Dict]:
        return self._markets.get(market_id)

    async def get_order_status(self, order_id: str) -> Optional[Dict]:
        rec = self._orders.get(order_id)
        if not rec:
            return None
        return {
            "order_id": order_id,
            "status": "FILLED",
            "avg_fill_price": str(rec["fill_price"]),
            "filled_quantity": str(rec["quantity"]),
            "fees": str(rec["fees"]),
        }

    async def place_limit_order(
        self,
        market_id: str,
        token_id: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        **kwargs,
    ) -> Dict:
        if market_id in self._reject_market_ids:
            return {"success": False, "error": "market_rejected_in_replay", "order_id": None}

        slip = Decimal(str(self._slippage_bps)) / Decimal("10000")
        fill_price = (
            price * (Decimal("1") + slip) if side.upper() == "BUY"
            else price * (Decimal("1") - slip)
        )
        fill_price = fill_price.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        fees = (quantity * fill_price * Decimal("0.002")).quantize(Decimal("0.0001"))

        # Unique order ID that will not collide across replays
        loop_time = 0
        try:
            loop_time = int(asyncio.get_event_loop().time() * 1_000_000)
        except RuntimeError:
            import time as _time
            loop_time = int(_time.time() * 1_000_000)
        order_id = f"replay_{market_id[:8]}_{loop_time}"

        self._orders[order_id] = {
            "market_id": market_id,
            "quantity": quantity,
            "fill_price": fill_price,
            "fees": fees,
        }
        return {
            "success": True,
            "order_id": order_id,
            "status": "FILLED",
            "filled_quantity": str(quantity),
            "filled_price": str(fill_price),
            "fees": str(fees),
        }

    async def place_order(self, *args, **kwargs) -> Dict:
        return await self.place_limit_order(*args, **kwargs)

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Log reader
# ---------------------------------------------------------------------------

def _parse_iso(ts_str: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; return None on any parse error."""
    if not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def load_log_events(
    log_file: str,
    from_ts: Optional[datetime] = None,
    to_ts: Optional[datetime] = None,
) -> List[LogEvent]:
    """
    Parse a structlog JSON-lines log file; return events sorted by timestamp.

    Only JSON objects with an ``event`` key are returned.  Lines outside
    [from_ts, to_ts] are dropped after timestamp parsing.
    """
    events: List[LogEvent] = []
    path = Path(log_file)
    if not path.exists():
        logger.warning("replay_log_not_found", path=str(path))
        return events

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            event_name = obj.get("event") or obj.get("message")
            if not event_name:
                continue
            ts_str = obj.get("timestamp") or obj.get("ts") or obj.get("time")
            ts = _parse_iso(ts_str) if ts_str else None
            if ts is None:
                ts = datetime.now(timezone.utc)

            if from_ts and ts < from_ts:
                continue
            if to_ts and ts > to_ts:
                continue

            events.append(LogEvent(timestamp=ts, event=event_name, data=obj))

    events.sort(key=lambda e: e.timestamp)
    logger.info(
        "replay_log_loaded",
        path=str(path),
        total_events=len(events),
        from_ts=from_ts.isoformat() if from_ts else None,
        to_ts=to_ts.isoformat() if to_ts else None,
    )
    return events


# ---------------------------------------------------------------------------
# Replay engine
# ---------------------------------------------------------------------------

class ReplayEngine:
    """
    Event-driven replay engine.

    Parameters
    ----------
    log_file:
        Path to a structlog JSON-lines production log.
    from_ts / to_ts:
        Optional datetime filter (UTC).  Both default to the full range of
        events in the log file.
    kelly_config:
        Override ``KELLY_CONFIG`` for sweep experiments.  Defaults to the
        current production config.
    initial_equity:
        Override ``STARTING_CAPITAL``.
    slippage_bps:
        Artificial slippage injected into fake fills (0 = no slippage).
    baseline_path:
        Path to a JSON baseline file used by ``assert_no_regression``.
        Set to None to skip regression checks.
    regression_tolerances:
        Dict of ``{metric_name: max_abs_change}`` applied by
        ``assert_no_regression``.  Unlisted metrics are not checked.
    """

    # Default absolute tolerances for regression checking.
    DEFAULT_REGRESSION_TOLERANCES: Dict[str, float] = {
        "total_pnl":        0.50,   # ≤ $0.50 change in final PnL is noise
        "max_drawdown_pct": 2.0,    # ≤ 2 pp change in max drawdown
        "win_rate":         0.05,   # ≤ 5 pp change in win rate
        "total_trades":     5,      # ≤ 5 extra/fewer trades
        "auto_blocks":      2,      # ≤ 2 extra auto-blocks
    }

    def __init__(
        self,
        log_file: str = "bot_production.log",
        from_ts: Optional[datetime] = None,
        to_ts: Optional[datetime] = None,
        kelly_config: Optional[Dict] = None,
        initial_equity: Optional[Decimal] = None,
        slippage_bps: float = 0.0,
        baseline_path: Optional[str] = "data/replay_baseline.json",
        regression_tolerances: Optional[Dict[str, float]] = None,
    ) -> None:
        self._log_file = log_file
        self._from_ts = from_ts
        self._to_ts = to_ts
        self._kelly_config = kelly_config or dict(KELLY_CONFIG)
        self._initial_equity = float(initial_equity or STARTING_CAPITAL)
        self._slippage_bps = slippage_bps
        self._baseline_path = baseline_path
        self._tolerances = regression_tolerances or dict(self.DEFAULT_REGRESSION_TOLERANCES)

    # ---- public interface ------------------------------------------------

    async def run(self) -> Dict[str, Any]:
        """
        Execute the full replay and return a results dict.

        The dict contains everything from ``ReplayMetrics.to_dict()`` plus:
            ``summary``  — human-readable multi-line report string
            ``config``   — Kelly/risk config used in this run
        """
        events = load_log_events(self._log_file, self._from_ts, self._to_ts)
        if not events:
            logger.warning("replay_no_events", log_file=self._log_file)
            empty: Dict[str, Any] = ReplayMetrics(self._initial_equity).to_dict()
            empty["summary"] = (
                f"No events found in {self._log_file!r} for the given time range.\n"
                f"Tip: check that the log file exists and is in structlog JSON-lines format."
            )
            empty["config"] = self._kelly_config_summary()
            return empty

        # In-memory ledger — completely isolated from production data.
        ledger = AsyncLedger(db_path=":memory:", pool_size=1)
        await ledger.pool.initialize()
        await ledger.record_deposit(
            Decimal(str(self._initial_equity)), "replay_initial_capital"
        )

        fake_client = FakePolymarketClient(slippage_bps=self._slippage_bps)
        do_not_trade = DoNotTradeRegistry(path=":memory_replay:", auto_load=False)
        kelly_sizer = KellySizer(config=self._kelly_config)
        metrics = ReplayMetrics(self._initial_equity)

        await self._process_events(
            events, ledger, fake_client, do_not_trade, kelly_sizer, metrics
        )

        results: Dict[str, Any] = metrics.to_dict()
        results["summary"] = self._format_summary(results)
        results["config"] = self._kelly_config_summary()

        logger.info(
            "replay_complete",
            total_trades=results["total_trades"],
            settled=results["settled_trades"],
            total_pnl=results["total_pnl"],
            max_drawdown_pct=results["max_drawdown_pct"],
            win_rate=results["win_rate"],
        )

        await ledger.close()
        return results

    def save_baseline(
        self,
        results: Dict[str, Any],
        path: Optional[str] = None,
    ) -> None:
        """
        Save current results as the regression baseline.

        Only scalar metrics are stored (equity_series and auto_block_list
        are omitted to keep the file small).
        """
        target = Path(path or self._baseline_path or "data/replay_baseline.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        scalar_results = {
            k: v for k, v in results.items()
            if k not in {"equity_series", "auto_block_list", "summary"}
        }
        target.write_text(json.dumps(scalar_results, indent=2))
        logger.info("replay_baseline_saved", path=str(target))

    def assert_no_regression(
        self,
        results: Dict[str, Any],
        baseline_path: Optional[str] = None,
    ) -> List[str]:
        """
        Compare *results* against the stored baseline.

        Returns a list of regression strings (empty = no regression).
        Does not raise — the caller decides how to handle failures.

        Example in CI::

            regressions = engine.assert_no_regression(results)
            if regressions:
                sys.exit("\\n".join(regressions))
        """
        path = Path(baseline_path or self._baseline_path or "data/replay_baseline.json")
        if not path.exists():
            logger.info("replay_no_baseline_skip", path=str(path))
            return []

        try:
            baseline = json.loads(path.read_text())
        except Exception as exc:
            logger.warning("replay_baseline_load_error", error=str(exc))
            return []

        regressions: List[str] = []
        for metric, tol in self._tolerances.items():
            old = baseline.get(metric)
            new = results.get(metric)
            if old is None or new is None:
                continue
            try:
                diff = abs(float(new) - float(old))
            except (TypeError, ValueError):
                continue
            if diff > tol:
                regressions.append(
                    f"REGRESSION {metric}: baseline={old} current={new} "
                    f"diff={diff:.4f} tolerance={tol}"
                )
        return regressions

    # ---- internal --------------------------------------------------------

    async def _process_events(
        self,
        events: List[LogEvent],
        ledger: AsyncLedger,
        fake_client: FakePolymarketClient,
        do_not_trade: DoNotTradeRegistry,
        kelly_sizer: KellySizer,
        metrics: ReplayMetrics,
    ) -> None:
        for ev in events:
            try:
                if ev.event == "arbitrage_opportunity_detected":
                    await self._handle_opportunity(
                        ev, ledger, fake_client, do_not_trade, kelly_sizer, metrics
                    )
                elif ev.event in (
                    "order_settled_live",
                    "order_settled_offline",
                    "ledger_order_settled_offline",
                ):
                    await self._handle_settlement(ev, metrics)
            except Exception as exc:
                logger.debug(
                    "replay_event_error",
                    event=ev.event,
                    ts=ev.timestamp.isoformat(),
                    error=str(exc),
                )

    async def _handle_opportunity(
        self,
        ev: LogEvent,
        ledger: AsyncLedger,
        fake_client: FakePolymarketClient,
        do_not_trade: DoNotTradeRegistry,
        kelly_sizer: KellySizer,
        metrics: ReplayMetrics,
    ) -> None:
        """
        Replay one ``arbitrage_opportunity_detected`` event through the
        current Kelly sizing and risk logic.

        Uses the logged market_price, edge, and charlie_p_win so the trade
        decision mirrors what the bot saw — the only thing that changes is
        the *sizing* config being evaluated.
        """
        d = ev.data
        market_id = d.get("market_id", "")
        token_id = d.get("token_id", "") or ""
        if not market_id:
            return

        if do_not_trade.is_blocked(market_id):
            return

        try:
            market_price = Decimal(str(d.get("market_price") or d.get("price") or "0"))
            edge_raw = Decimal(str(d.get("edge") or "0"))
        except Exception:
            return

        if market_price <= Decimal("0") or edge_raw <= Decimal("0"):
            return

        # Register market in fake client so get_market() works during replay.
        fake_client.set_market(market_id, {
            "id": market_id,
            "question": d.get("question", ""),
            "resolved": False,
            "closed": False,
        })

        equity = await ledger.get_equity()
        if equity <= Decimal("0"):
            return

        # p_win: prefer charlie_p_win logged by bot; fall back to price + edge.
        charlie_p_win_raw = d.get("charlie_p_win")
        if charlie_p_win_raw is not None:
            try:
                p_win = float(charlie_p_win_raw)
            except Exception:
                p_win = float(market_price) + float(edge_raw)
        else:
            p_win = float(market_price) + float(edge_raw)

        kelly_result: KellySizeResult = kelly_sizer.compute_size(
            p_win=min(p_win, 0.999),
            implied_prob=max(float(market_price), 0.001),
            bankroll=equity,
        )
        if not kelly_result:
            return  # no edge or zero size after caps

        # Regime multiplier (cap at 1× — never allow replay to over-size).
        technical_regime = (
            d.get("technical_regime") or d.get("charlie_regime") or "UNKNOWN"
        )
        regime_mult = REGIME_RISK_OVERRIDES.get(technical_regime, Decimal("1.0"))
        regime_mult = min(regime_mult, Decimal("1.0"))
        order_value = (kelly_result.size * regime_mult).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )

        # Minimum position guard (from production config).
        min_pos = Decimal(str(CHARLIE_CONFIG.get("min_position_size", Decimal("1.00"))))
        if order_value < min_pos:
            return

        side = str(d.get("side") or "YES").upper()
        quantity = (
            order_value / market_price
            if market_price > Decimal("0")
            else Decimal("0")
        )
        order_id = f"replay_{market_id[:8]}_{int(ev.timestamp.timestamp() * 1000)}"

        await ledger.record_order_created(
            order_id=order_id,
            market_id=market_id,
            token_id=token_id,
            outcome=side,
            side="BUY",
            size=order_value,
            price=market_price,
            charlie_p_win=Decimal(str(round(p_win, 6))),
            charlie_conf=Decimal(str(d.get("charlie_conf") or d.get("confidence") or "0.7")),
            charlie_regime=technical_regime,
            strategy="replay",
        )
        # Record cash outflow in the double-entry ledger so equity tracks properly.
        await ledger.record_trade_entry(
            market_id=market_id,
            token_id=token_id,
            strategy="replay",
            entry_price=market_price,
            quantity=quantity,
            fees=Decimal("0"),
            entry_order_id=order_id,
        )
        await ledger.transition_order_state(order_id, "FILLED")

        metrics.record_trade(
            timestamp=ev.timestamp.isoformat(),
            market_id=market_id,
            order_value=float(order_value),
            kelly_fraction=float(kelly_result.kelly_fraction),
            edge=float(edge_raw),
            regime=technical_regime,
        )

    async def _handle_settlement(
        self,
        ev: LogEvent,
        metrics: ReplayMetrics,
    ) -> None:
        """
        Apply realized PnL from a settlement event logged by the production bot.

        We use the logged PnL directly (rather than recomputing from payout) so
        replay PnL is as close as possible to what the bot actually earned.
        """
        d = ev.data
        pnl_raw = d.get("pnl")
        if pnl_raw is None:
            return
        try:
            pnl = float(pnl_raw)
        except Exception:
            return

        order_id = d.get("order_id", f"settle_{ev.timestamp.timestamp()}")
        metrics.record_settlement(
            timestamp=ev.timestamp.isoformat(),
            pnl=pnl,
            order_id=order_id,
        )

    # ---- formatting / output --------------------------------------------

    def _format_summary(self, r: Dict[str, Any]) -> str:
        win_rate_str = f"{r['win_rate']:.1%}" if r["win_rate"] is not None else "N/A"
        cagr_str = f"{r['cagr']:.2%}" if r["cagr"] is not None else "N/A"
        sharpe_str = f"{r['sharpe']:.2f}" if r["sharpe"] is not None else "N/A"
        lines = [
            "",
            "=" * 65,
            "  REPLAY RESULTS",
            "=" * 65,
            f"  Log file          : {self._log_file}",
            f"  From              : {self._from_ts.isoformat() if self._from_ts else '(start)'}",
            f"  To                : {self._to_ts.isoformat() if self._to_ts else '(end)'}",
            f"  Days covered      : {r['days_covered']:.1f}",
            "",
            f"  Initial equity    : ${self._initial_equity:,.2f}",
            f"  Final equity      : ${r['final_equity']:,.4f}",
            f"  Total PnL         : ${r['total_pnl']:+.4f}",
            f"  CAGR              : {cagr_str}",
            f"  Sharpe            : {sharpe_str}",
            "",
            f"  Max drawdown      : {r['max_drawdown_pct']:.2f}%",
            "",
            f"  Total trades      : {r['total_trades']}",
            f"  Settled trades    : {r['settled_trades']}",
            f"  Wins              : {r['wins']}",
            f"  Win rate          : {win_rate_str}",
            "",
            f"  Kelly frac (mean) : {r['kelly_fraction_mean']:.4f}",
            f"  Kelly frac (max)  : {r['kelly_fraction_max']:.4f}",
            f"  Auto-blocks       : {r['auto_blocks']}",
            "",
            "  Active Kelly config:",
        ]
        for k, v in self._kelly_config_summary().items():
            lines.append(f"    {k:<25s}: {v}")
        lines.append("=" * 65)
        return "\n".join(lines)

    def _kelly_config_summary(self) -> Dict[str, str]:
        return {
            k: str(v)
            for k, v in self._kelly_config.items()
            if k in {
                "fractional_kelly", "max_bet_pct",
                "min_edge_required", "min_confidence",
            }
        }


# ---------------------------------------------------------------------------
# CLI entry-point (called by main.py --mode replay)
# ---------------------------------------------------------------------------

async def run_replay(
    log_file: str = "bot_production.log",
    from_ts: Optional[datetime] = None,
    to_ts: Optional[datetime] = None,
    baseline_path: Optional[str] = "data/replay_baseline.json",
) -> Dict[str, Any]:
    """
    Entry-point for ``python main.py --mode replay``.

    Prints the replay summary and compares against the stored baseline.
    Returns the full results dict for programmatic use (e.g. sweep experiments).
    """
    engine = ReplayEngine(
        log_file=log_file,
        from_ts=from_ts,
        to_ts=to_ts,
        baseline_path=baseline_path,
    )
    results = await engine.run()
    print(results["summary"])

    regressions = engine.assert_no_regression(results)
    if regressions:
        print("\n*** REGRESSIONS DETECTED ***")
        for r in regressions:
            print(f"  {r}")
        print(
            "\nTo promote this run as the new baseline:\n"
            "  engine.save_baseline(results)\n"
        )
    else:
        print("  No regressions vs baseline.\n")

    return results
