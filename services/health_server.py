"""
Minimal HTTP health check server for the Polymarket bot.

Serves GET /health on port 8765 (configurable via HEALTH_PORT env var).
Returns JSON with live bot state.

HTTP 200 = healthy
HTTP 503 = degraded (circuit breaker OPEN or last_scan_ago_s > 120)

Usage from PowerShell:
    while (1) { Invoke-RestMethod http://localhost:8765/health | ConvertTo-Json; sleep 10 }

Wire into main.py:
    from services.health_server import HealthServer
    hs = HealthServer(state_ref=system)
    asyncio.create_task(hs.serve())
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import TYPE_CHECKING, Any, Dict

import structlog

if TYPE_CHECKING:
    pass  # forward ref for TradingSystem to avoid circular import

logger = structlog.get_logger(__name__)

_DEFAULT_PORT = int(os.environ.get("HEALTH_PORT", 8765))
_MAX_SCAN_STALENESS_S = 120


class HealthServer:
    """
    Background asyncio HTTP server.

    Parameters
    ----------
    state_ref:
        Reference to the TradingSystem instance.  Reads attributes like
        ``last_heartbeat_at``, ``start_time``, ``circuit_breaker``, etc.
    port:
        TCP port to listen on.  Default: 8765 (or HEALTH_PORT env var).
    """

    def __init__(self, state_ref: Any, port: int = _DEFAULT_PORT) -> None:
        self._state = state_ref
        self._port  = port
        self._scan_count = 0
        self._last_scan_ts: float = 0.0

    def record_scan(self) -> None:
        """Call once per strategy scan so the health endpoint tracks freshness."""
        self._scan_count += 1
        self._last_scan_ts = time.monotonic()

    async def serve(self) -> None:
        """Start the HTTP server (runs until cancelled)."""
        try:
            server = await asyncio.start_server(
                self._handle, "0.0.0.0", self._port
            )
            logger.info("health_server_started", port=self._port)
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            logger.info("health_server_stopped")
        except OSError as exc:
            logger.warning("health_server_bind_failed",
                           port=self._port, error=str(exc))

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            # Minimal HTTP parse — we only care that it's a GET /health
            raw = await asyncio.wait_for(reader.read(512), timeout=3.0)
            request_line = raw.decode("utf-8", errors="replace").split("\r\n")[0]

            payload = await self._build_payload()
            healthy = payload["status"] == "ok"
            status_line = "HTTP/1.1 200 OK" if healthy else "HTTP/1.1 503 Service Unavailable"
            body = json.dumps(payload, indent=2)
            response = (
                f"{status_line}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body.encode())}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{body}"
            )
            writer.write(response.encode())
            await writer.drain()
        except Exception as exc:
            logger.debug("health_handler_error", error=str(exc))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _build_payload(self) -> Dict:
        state = self._state
        now = time.monotonic()

        uptime_s = round(now - getattr(state, "start_time", now), 1)
        last_scan_ago_s = round(now - self._last_scan_ts, 1) if self._last_scan_ts else None

        # Equity
        equity_usdc = None
        try:
            if getattr(state, "ledger", None) is not None:
                eq = await asyncio.wait_for(state.ledger.get_equity(), timeout=2.0)
                equity_usdc = float(eq) if eq is not None else None
        except Exception:
            pass

        # Open positions count
        open_positions = None
        try:
            if getattr(state, "ledger", None) is not None:
                rows = await asyncio.wait_for(state.ledger.get_open_orders(), timeout=2.0)
                open_positions = len(rows) if rows else 0
        except Exception:
            pass

        # Circuit breakers (trading risk)
        cb_state = "unknown"
        try:
            cb = getattr(state, "circuit_breaker", None)
            if cb is not None:
                cb_state = cb.state.value if hasattr(cb.state, "value") else str(cb.state)
        except Exception:
            pass

        # Service circuit breakers
        from services.circuit_breaker import cb_gamma, cb_clob, cb_binance, cb_charlie
        svc_cbs = {
            "gamma":   cb_gamma.status,
            "clob":    cb_clob.status,
            "binance": cb_binance.status,
            "charlie": cb_charlie.status,
        }

        # Drawdown
        drawdown_d = getattr(state, "drawdown_monitor", None)
        drawdown_halted = getattr(drawdown_d, "trading_halted", False) if drawdown_d else False

        # Determine health
        svc_open = any(v == "open" for v in svc_cbs.values())
        scan_stale = last_scan_ago_s is not None and last_scan_ago_s > _MAX_SCAN_STALENESS_S
        status = "ok" if (not svc_open and not scan_stale and not drawdown_halted) else "degraded"

        return {
            "status":              status,
            "uptime_s":            uptime_s,
            "scan_count":          self._scan_count,
            "last_scan_ago_s":     last_scan_ago_s,
            "open_positions":      open_positions,
            "equity_usdc":         equity_usdc,
            "trading_circuit_breaker": cb_state,
            "service_circuit_breakers": svc_cbs,
            "drawdown_halted":     drawdown_halted,
        }
