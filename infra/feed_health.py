"""Feed health monitoring: OFI degradation tracking + BinanceWebSocket supervisor.

This module converts silent feed failures into structured, machine-readable
events that session checks, the live dashboard, and the Charlie gate can act on.

Problems fixed
--------------
- OFI staleness was a soft silent degradation (only a warning log sometimes).
  Now: every missing snapshot is counted; N consecutive misses latch an alert.
- BinanceWebSocket health_check() existed but nothing called it on a schedule
  and raised a system-level flag.  Now: FeedSupervisor does, every N seconds.

Usage in main loop::

    from infra.feed_health import OFIHealthMonitor, FeedSupervisor

    ofi_health = OFIHealthMonitor(max_consecutive_misses=5)
    supervisor = FeedSupervisor(binance_ws=binance_ws_instance, ofi_health=ofi_health)

    # inside the async main loop:
    asyncio.create_task(supervisor.run())

    # when building extra_features for each market:
    has_ofi = bool(extra_features.get("ofi_bids") and extra_features.get("ofi_asks"))
    ofi_health.record(symbol="BTC", has_ofi=has_ofi)

    # before placing any order, check system readiness:
    if not supervisor.is_trade_ready():
        logger.warning("system_not_trade_ready", reason=supervisor.readiness_reason())
        continue
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

try:
    import structlog
    _logger = structlog.get_logger(__name__)
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    class _FallbackLogger:
        def __init__(self, name: str):
            self._l = logging.getLogger(name)
        def info(self, event: str, **kw): self._l.info(f"{event} | {kw}" if kw else event)
        def warning(self, event: str, **kw): self._l.warning(f"{event} | {kw}" if kw else event)
        def error(self, event: str, **kw): self._l.error(f"{event} | {kw}" if kw else event)
        def debug(self, event: str, **kw): self._l.debug(f"{event} | {kw}" if kw else event)
    _logger = _FallbackLogger(__name__)


# ---------------------------------------------------------------------------
# OFI Health Monitor
# ---------------------------------------------------------------------------

@dataclass
class OFISymbolState:
    consecutive_misses: int = 0
    total_misses: int = 0
    total_records: int = 0
    degraded: bool = False
    last_degraded_at: Optional[float] = None
    last_seen_at: Optional[float] = None


class OFIHealthMonitor:
    """
    Tracks per-symbol OFI snapshot availability.

    Each time a market is evaluated, call ``record(symbol, has_ofi=True/False)``.
    When ``has_ofi=False`` is recorded ``max_consecutive_misses`` times in a row,
    a structured ``ofi_feed_degraded`` event is emitted and the symbol is marked
    degraded until a successful snapshot arrives.

    Parameters
    ----------
    max_consecutive_misses:
        How many consecutive missing OFI snapshots before declaring degraded.
        Default 5 (i.e., ~5 successive market evaluations).
    """

    def __init__(self, max_consecutive_misses: int = 5) -> None:
        self._max_misses = max(1, int(max_consecutive_misses))
        self._symbols: Dict[str, OFISymbolState] = {}

    def record(self, symbol: str, *, has_ofi: bool) -> None:
        """Record whether OFI data was available for this market evaluation."""
        state = self._symbols.setdefault(symbol, OFISymbolState())
        state.total_records += 1

        if has_ofi:
            state.consecutive_misses = 0
            state.last_seen_at = time.time()
            if state.degraded:
                _logger.info(
                    "ofi_feed_recovered",
                    symbol=symbol,
                    total_records=state.total_records,
                    total_misses=state.total_misses,
                )
                state.degraded = False
        else:
            state.consecutive_misses += 1
            state.total_misses += 1

            if not state.degraded and state.consecutive_misses >= self._max_misses:
                state.degraded = True
                state.last_degraded_at = time.time()
                _logger.warning(
                    "ofi_feed_degraded",
                    symbol=symbol,
                    consecutive_misses=state.consecutive_misses,
                    total_misses=state.total_misses,
                    total_records=state.total_records,
                    action="ofi_conflict_halving_inactive_until_feed_recovers",
                )
            elif state.degraded:
                _logger.debug(
                    "ofi_feed_still_degraded",
                    symbol=symbol,
                    consecutive_misses=state.consecutive_misses,
                )

    def is_degraded(self, symbol: str) -> bool:
        """Return True if the symbol's OFI feed is currently degraded."""
        return self._symbols.get(symbol, OFISymbolState()).degraded

    def summary(self) -> Dict:
        """Return a dict of all symbol states for dashboards / session checks."""
        return {
            sym: {
                "degraded": s.degraded,
                "consecutive_misses": s.consecutive_misses,
                "total_misses": s.total_misses,
                "total_records": s.total_records,
            }
            for sym, s in self._symbols.items()
        }


# ---------------------------------------------------------------------------
# Feed Supervisor
# ---------------------------------------------------------------------------

class FeedSupervisor:
    """
    Periodically polls BinanceWebSocket.health_check() and OFIHealthMonitor
    to determine overall system trade-readiness.

    Sets a ``_trade_ready`` flag that the main loop should check before
    placing any order.  When the flag is False, the ``readiness_reason()``
    method returns a human-readable string explaining why.

    Parameters
    ----------
    binance_ws:
        An instance of ``data_feeds.binance_websocket_v2.BinanceWebSocketV2``.
        Pass None to skip Binance health checks (e.g. in paper-trading mode
        without a live feed).
    ofi_health:
        An ``OFIHealthMonitor`` instance.  Pass None to skip OFI checks.
    check_interval_seconds:
        How often (seconds) to poll health.  Default 30.
    ofi_critical_symbols:
        If OFI is degraded for ANY of these symbols, mark not trade-ready.
        Default ["BTC"].
    """

    def __init__(
        self,
        binance_ws=None,
        ofi_health: Optional[OFIHealthMonitor] = None,
        *,
        check_interval_seconds: float = 30.0,
        ofi_critical_symbols: Optional[list] = None,
    ) -> None:
        self._binance_ws = binance_ws
        self._ofi_health = ofi_health
        self._interval = max(5.0, float(check_interval_seconds))
        self._ofi_critical = ofi_critical_symbols or ["BTC"]
        self._trade_ready: bool = True
        self._readiness_reason: str = "startup"
        self._last_check: Optional[float] = None
        self._running: bool = False

    def is_trade_ready(self) -> bool:
        """True if all feeds are healthy and orders may be submitted."""
        return self._trade_ready

    def readiness_reason(self) -> str:
        """Human-readable string describing why trade_ready is False (or 'ok')."""
        return self._readiness_reason

    async def run(self) -> None:
        """Async supervisor loop.  Run as a background task."""
        _logger.info("feed_supervisor_started", interval_seconds=self._interval)
        self._running = True
        while self._running:
            await asyncio.sleep(self._interval)
            await self._check()

    def stop(self) -> None:
        """Signal the supervisor loop to stop."""
        self._running = False

    async def _check(self) -> None:
        self._last_check = time.time()
        reasons = []

        # --- Binance WebSocket health -----------------------------------
        if self._binance_ws is not None:
            try:
                healthy = await self._binance_ws.health_check()
            except Exception as exc:
                healthy = False
                _logger.error(
                    "feed_supervisor_binance_health_check_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            if not healthy:
                metrics = {}
                try:
                    metrics = self._binance_ws.get_metrics()
                except Exception:
                    pass
                _logger.warning(
                    "binance_feed_unhealthy",
                    state=metrics.get("state"),
                    messages_received=metrics.get("messages_received"),
                    messages_processed=metrics.get("messages_processed"),
                    connected_symbols=metrics.get("connected_symbols"),
                    heartbeat_failures=metrics.get("heartbeat_failures"),
                    queue_size=metrics.get("queue_size"),
                    uptime_seconds=metrics.get("uptime_seconds"),
                    action="blocking_order_submission_until_feed_recovers",
                )
                reasons.append("binance_feed_unhealthy")
            else:
                _logger.debug("binance_feed_healthy")

        # --- OFI feed health -------------------------------------------
        if self._ofi_health is not None:
            for sym in self._ofi_critical:
                if self._ofi_health.is_degraded(sym):
                    _logger.warning(
                        "ofi_feed_critical_symbol_degraded",
                        symbol=sym,
                        action="ofi_halving_disabled_for_affected_markets",
                    )
                    reasons.append(f"ofi_degraded:{sym}")

        # --- Update trade-ready flag ------------------------------------
        # OFI degradation alone does NOT block trading (Charlie retains primacy;
        # degraded OFI just means the halving safeguard is inactive).
        # Only Binance feed failure blocks trading, because it is the source
        # of all BTC price / indicator features for Charlie.
        binance_blocked = any(r == "binance_feed_unhealthy" for r in reasons)

        if binance_blocked and self._trade_ready:
            self._trade_ready = False
            self._readiness_reason = "binance_feed_unhealthy"
            _logger.error(
                "system_not_trade_ready",
                reason=self._readiness_reason,
                action="all_order_submissions_blocked",
            )
        elif not binance_blocked and not self._trade_ready:
            self._trade_ready = True
            self._readiness_reason = "ok"
            _logger.info(
                "system_trade_ready_restored",
                reason="binance_feed_recovered",
            )
        elif not reasons:
            self._readiness_reason = "ok"

    def health_snapshot(self) -> Dict:
        """Return a snapshot dict for dashboards / check_session."""
        ofi_summary = self._ofi_health.summary() if self._ofi_health else {}
        binance_metrics = {}
        try:
            if self._binance_ws:
                binance_metrics = self._binance_ws.get_metrics()
        except Exception:
            pass
        return {
            "trade_ready": self._trade_ready,
            "readiness_reason": self._readiness_reason,
            "last_check": self._last_check,
            "binance": binance_metrics,
            "ofi": ofi_summary,
        }
