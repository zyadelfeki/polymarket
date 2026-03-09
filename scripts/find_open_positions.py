#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from data_feeds.polymarket_client_v2 import PolymarketClientV2
from database.ledger_async import AsyncLedger

OPEN_ORDER_STATES = frozenset({"CREATED", "SUBMITTED", "PARTIALLY_FILLED", "FILLED"})
TERMINAL_ORDER_STATES = frozenset({"CANCELLED", "EXPIRED", "SETTLED", "ERROR", "SUPERSEDED"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Report open orders and current positions from the configured ledger DB, "
            "with optional read-only exchange enrichment."
        )
    )
    parser.add_argument("--config", default="config/production.yaml", help="Path to YAML config file")
    parser.add_argument("--db-path", default=None, help="Override ledger DB path from config")
    parser.add_argument(
        "--exchange",
        choices=("config", "live", "off"),
        default="config",
        help=(
            "Exchange lookup mode: 'config' uses trading.paper_trading from config, "
            "'live' forces a read-only live client, 'off' skips exchange calls."
        ),
    )
    return parser.parse_args(argv)


def load_config(config_path: str) -> dict[str, Any]:
    with open(Path(config_path), "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


def resolve_db_path(config: dict[str, Any], override: str | None) -> str:
    if override:
        return override
    return str(config.get("database", {}).get("path", "data/trading.db"))


def resolve_effective_paper_trading(config: dict[str, Any], exchange_mode: str) -> bool | None:
    configured = bool(config.get("trading", {}).get("paper_trading", True))
    if exchange_mode == "off":
        return None
    if exchange_mode == "live":
        return False
    return configured


def build_api_client(config: dict[str, Any], exchange_mode: str) -> PolymarketClientV2 | None:
    effective_paper_trading = resolve_effective_paper_trading(config, exchange_mode)
    if effective_paper_trading is None:
        return None

    api_config = config.get("api", {}).get("polymarket", {})
    return PolymarketClientV2(
        api_key=os.getenv("POLYMARKET_API_KEY"),
        private_key=None if effective_paper_trading else os.getenv("POLYMARKET_PRIVATE_KEY"),
        paper_trading=effective_paper_trading,
        rate_limit=api_config.get("rate_limit", 8.0),
        timeout=api_config.get("timeout_seconds", 10.0),
        max_retries=api_config.get("max_retries", 3),
    )


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _decimal_to_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return format(normalized, "f")


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _safe_upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: Any) -> int | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


async def collect_local_state(ledger: AsyncLedger) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in OPEN_ORDER_STATES)
    open_orders = await ledger.execute(
        f"""
        SELECT
            ot.order_id,
            ot.market_id,
            ot.token_id,
            ot.outcome,
            ot.side,
            ot.size,
            ot.price,
            ot.order_state,
            ot.opened_at,
            ot.closed_at,
            ot.pnl,
            ot.notes,
            ot.expected_price,
            ot.filled_price,
            ot.slippage_bps,
            il.status AS idempotency_status,
            il.filled_quantity AS idempotency_filled_quantity,
            il.filled_price AS idempotency_filled_price,
            il.fees AS idempotency_fees
        FROM order_tracking ot
        LEFT JOIN idempotency_log il
          ON il.id = (
              SELECT il2.id
              FROM idempotency_log il2
              WHERE il2.order_id = ot.order_id
              ORDER BY il2.updated_at DESC, il2.id DESC
              LIMIT 1
          )
        WHERE ot.order_state IN ({placeholders})
        ORDER BY ot.opened_at ASC, ot.order_id ASC
        """,
        tuple(OPEN_ORDER_STATES),
        fetch_all=True,
        as_dict=True,
    ) or []

    open_positions = await ledger.execute(
        """
        SELECT
            market_id,
            token_id,
            COALESCE(MAX(side), '') AS side,
            COUNT(*) AS open_lots,
            SUM(CAST(quantity AS REAL)) AS quantity,
            SUM(CAST(quantity AS REAL) * CAST(entry_price AS REAL))
                / NULLIF(SUM(CAST(quantity AS REAL)), 0) AS entry_price,
            SUM(
                CASE WHEN current_price IS NOT NULL THEN CAST(quantity AS REAL) * CAST(current_price AS REAL)
                ELSE NULL END
            ) / NULLIF(
                SUM(CASE WHEN current_price IS NOT NULL THEN CAST(quantity AS REAL) ELSE NULL END),
                0
            ) AS current_price,
            SUM(CAST(unrealized_pnl AS REAL)) AS stored_unrealized_pnl,
            MAX(strategy) AS strategy,
            MIN(COALESCE(opened_at, entry_timestamp)) AS opened_at,
            GROUP_CONCAT(COALESCE(entry_order_id, ''), ',') AS entry_order_ids
        FROM positions
        WHERE status = 'OPEN'
        GROUP BY market_id, token_id
        ORDER BY opened_at ASC, market_id ASC, token_id ASC
        """,
        fetch_all=True,
        as_dict=True,
    ) or []

    order_tracking_count = await ledger.execute_scalar("SELECT COUNT(*) FROM order_tracking") or 0
    positions_count = await ledger.execute_scalar("SELECT COUNT(*) FROM positions") or 0
    idempotency_count = await ledger.execute_scalar("SELECT COUNT(*) FROM idempotency_log") or 0

    return {
        "open_orders": open_orders,
        "open_positions": open_positions,
        "table_counts": {
            "order_tracking": int(order_tracking_count),
            "positions": int(positions_count),
            "idempotency_log": int(idempotency_count),
        },
    }


async def collect_exchange_state(client: PolymarketClientV2 | None, effective_paper_trading: bool | None) -> tuple[dict[str, Any], list[str]]:
    if client is None:
        return {"open_orders": [], "open_positions": []}, ["exchange lookup disabled (--exchange off)"]
    if effective_paper_trading:
        return {"open_orders": [], "open_positions": []}, [
            "exchange lookup skipped because effective paper_trading=true; use --exchange live for read-only live discovery"
        ]

    gaps: list[str] = []
    open_orders = []
    open_positions = []

    try:
        open_orders = await _maybe_await(client.get_open_orders()) or []
    except Exception as exc:
        gaps.append(f"exchange open-order lookup failed: {exc}")

    try:
        open_positions = await _maybe_await(client.get_open_positions()) or []
    except Exception as exc:
        gaps.append(f"exchange position lookup failed: {exc}")

    return {
        "open_orders": open_orders,
        "open_positions": open_positions,
    }, gaps


async def build_market_cache(client: PolymarketClientV2 | None, market_ids: Iterable[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if client is None:
        return {}, []

    cache: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    for market_id in sorted({str(mid) for mid in market_ids if str(mid)}):
        try:
            market = await _maybe_await(client.get_market(market_id))
        except Exception as exc:
            gaps.append(f"market lookup failed for {market_id}: {exc}")
            continue
        if not isinstance(market, dict):
            gaps.append(f"market lookup returned unsupported payload for {market_id}")
            continue
        cache[market_id] = market
    return cache, gaps


def _select_market_price(market: dict[str, Any] | None, token_id: str, outcome: str, side: str) -> Decimal | None:
    if not market:
        return None

    yes_token_id = str(market.get("yes_token_id") or "")
    no_token_id = str(market.get("no_token_id") or "")
    yes_price = _to_decimal(market.get("yes_price"))
    no_price = _to_decimal(market.get("no_price"))

    if token_id and token_id == yes_token_id:
        return yes_price
    if token_id and token_id == no_token_id:
        return no_price

    direction = _safe_upper(_coalesce(outcome, side))
    if direction == "YES":
        return yes_price
    if direction == "NO":
        return no_price
    return None


def _build_order_row(
    *,
    order_id: str,
    local_row: dict[str, Any] | None,
    exchange_row: dict[str, Any] | None,
    market_cache: dict[str, dict[str, Any]],
    exchange_compared: bool,
) -> dict[str, Any]:
    market_id = str(_coalesce(local_row.get("market_id") if local_row else None, exchange_row.get("market_id") if exchange_row else None, ""))
    token_id = str(_coalesce(local_row.get("token_id") if local_row else None, exchange_row.get("token_id") if exchange_row else None, ""))
    side = str(_coalesce(local_row.get("side") if local_row else None, exchange_row.get("side") if exchange_row else None, ""))
    outcome = str(_coalesce(local_row.get("outcome") if local_row else None, exchange_row.get("outcome") if exchange_row else None, side))
    order_state = _safe_upper(_coalesce(local_row.get("order_state") if local_row else None, exchange_row.get("status") if exchange_row else None, "UNKNOWN"))
    size = _to_decimal(_coalesce(local_row.get("size") if local_row else None, exchange_row.get("size") if exchange_row else None))
    entry_price = _to_decimal(_coalesce(local_row.get("price") if local_row else None, exchange_row.get("price") if exchange_row else None))
    current_price = _select_market_price(market_cache.get(market_id), token_id, outcome, side)
    shares = None
    if size is not None and entry_price is not None and entry_price > 0:
        shares = size / entry_price

    observations: list[str] = []
    if order_state not in OPEN_ORDER_STATES and order_state not in TERMINAL_ORDER_STATES:
        observations.append("unknown_order_state")
    if order_state == "FILLED":
        observations.append("filled_state_still_in_open_window")
    if local_row and _safe_upper(local_row.get("idempotency_status")) == "ERROR":
        observations.append("idempotency_error")
    if exchange_compared and local_row and not exchange_row:
        observations.append("local_open_order_missing_from_exchange_open_orders")
    if exchange_compared and exchange_row and not local_row:
        observations.append("exchange_open_order_missing_from_local_tracking")

    return {
        "order_id": order_id,
        "market_id": market_id or None,
        "token_id": token_id or None,
        "outcome": outcome or None,
        "side": side or None,
        "size": _decimal_to_text(size),
        "shares": _decimal_to_text(shares),
        "entry_reference_price": _decimal_to_text(entry_price),
        "current_reference_price": _decimal_to_text(current_price),
        "opened_at": _coalesce(local_row.get("opened_at") if local_row else None, exchange_row.get("opened_at") if exchange_row else None),
        "age_seconds": _age_seconds(_coalesce(local_row.get("opened_at") if local_row else None, exchange_row.get("opened_at") if exchange_row else None)),
        "local_order_state": local_row.get("order_state") if local_row else None,
        "exchange_order_state": exchange_row.get("status") if exchange_row else None,
        "idempotency_status": local_row.get("idempotency_status") if local_row else None,
        "filled_quantity": _decimal_to_text(_to_decimal(local_row.get("idempotency_filled_quantity") if local_row else None)),
        "filled_price": _decimal_to_text(_to_decimal(_coalesce(local_row.get("filled_price") if local_row else None, local_row.get("idempotency_filled_price") if local_row else None))),
        "notes": local_row.get("notes") if local_row else None,
        "observations": observations,
        "source": {
            "local_tracking": bool(local_row),
            "exchange_open_orders": bool(exchange_row),
        },
    }


def _position_key(row: dict[str, Any], fallback_prefix: str, index: int) -> str:
    market_id = str(row.get("market_id") or "")
    token_id = str(row.get("token_id") or "")
    if market_id or token_id:
        return f"{market_id}|{token_id}"
    return f"{fallback_prefix}:{index}"


def _build_position_row(
    *,
    key: str,
    local_row: dict[str, Any] | None,
    exchange_row: dict[str, Any] | None,
    market_cache: dict[str, dict[str, Any]],
    exchange_compared: bool,
) -> dict[str, Any]:
    market_id = str(_coalesce(local_row.get("market_id") if local_row else None, exchange_row.get("market_id") if exchange_row else None, ""))
    token_id = str(_coalesce(local_row.get("token_id") if local_row else None, exchange_row.get("token_id") if exchange_row else None, ""))
    side = str(_coalesce(local_row.get("side") if local_row else None, exchange_row.get("side") if exchange_row else None, exchange_row.get("outcome") if exchange_row else None, ""))
    quantity = _to_decimal(_coalesce(local_row.get("quantity") if local_row else None, exchange_row.get("quantity") if exchange_row else None, exchange_row.get("shares") if exchange_row else None, exchange_row.get("size") if exchange_row else None))
    entry_price = _to_decimal(_coalesce(local_row.get("entry_price") if local_row else None, exchange_row.get("entry_price") if exchange_row else None, exchange_row.get("avg_price") if exchange_row else None, exchange_row.get("price") if exchange_row else None))
    local_current_price = _to_decimal(local_row.get("current_price") if local_row else None)
    current_price = local_current_price or _select_market_price(market_cache.get(market_id), token_id, side, side)

    unrealized_pnl = None
    if quantity is not None and entry_price is not None and current_price is not None:
        unrealized_pnl = (current_price - entry_price) * quantity

    observations: list[str] = []
    if exchange_compared and local_row and not exchange_row:
        observations.append("local_position_missing_from_exchange_positions")
    if exchange_compared and exchange_row and not local_row:
        observations.append("exchange_position_missing_from_local_ledger")

    return {
        "position_key": key,
        "market_id": market_id or None,
        "token_id": token_id or None,
        "side": side or None,
        "size": _decimal_to_text(quantity),
        "entry_reference_price": _decimal_to_text(entry_price),
        "current_reference_price": _decimal_to_text(current_price),
        "unrealized_pnl": _decimal_to_text(unrealized_pnl),
        "strategy": local_row.get("strategy") if local_row else None,
        "open_lots": int(local_row.get("open_lots") or 0) if local_row else None,
        "opened_at": _coalesce(local_row.get("opened_at") if local_row else None, exchange_row.get("opened_at") if exchange_row else None),
        "age_seconds": _age_seconds(_coalesce(local_row.get("opened_at") if local_row else None, exchange_row.get("opened_at") if exchange_row else None)),
        "stored_unrealized_pnl": _decimal_to_text(_to_decimal(local_row.get("stored_unrealized_pnl") if local_row else None)),
        "observations": observations,
        "source": {
            "local_positions": bool(local_row),
            "exchange_positions": bool(exchange_row),
        },
    }


def build_report(
    *,
    config_path: str,
    db_path: str,
    configured_paper_trading: bool,
    effective_paper_trading: bool | None,
    exchange_mode: str,
    local_state: dict[str, Any],
    exchange_state: dict[str, Any],
    market_cache: dict[str, dict[str, Any]],
    extra_gaps: list[str],
) -> dict[str, Any]:
    exchange_compared = effective_paper_trading is False

    local_orders = {
        str(row.get("order_id") or f"local:{index}"): row
        for index, row in enumerate(local_state["open_orders"], start=1)
    }
    exchange_orders = {
        str(row.get("order_id") or f"exchange:{index}"): row
        for index, row in enumerate(exchange_state["open_orders"], start=1)
    }
    order_ids = sorted(set(local_orders) | set(exchange_orders))
    open_orders = [
        _build_order_row(
            order_id=order_id,
            local_row=local_orders.get(order_id),
            exchange_row=exchange_orders.get(order_id),
            market_cache=market_cache,
            exchange_compared=exchange_compared,
        )
        for order_id in order_ids
    ]

    local_positions = {
        _position_key(row, "local", index): row
        for index, row in enumerate(local_state["open_positions"], start=1)
    }
    exchange_positions = {
        _position_key(row, "exchange", index): row
        for index, row in enumerate(exchange_state["open_positions"], start=1)
    }
    position_keys = sorted(set(local_positions) | set(exchange_positions))
    positions = [
        _build_position_row(
            key=key,
            local_row=local_positions.get(key),
            exchange_row=exchange_positions.get(key),
            market_cache=market_cache,
            exchange_compared=exchange_compared,
        )
        for key in position_keys
    ]

    gaps = list(extra_gaps)
    if not local_state["open_orders"]:
        gaps.append("local order_tracking has no rows in open states")
    if not local_state["open_positions"]:
        gaps.append("local positions table has no OPEN rows")
    if exchange_compared and not exchange_state["open_orders"]:
        gaps.append("exchange lookup returned no open orders")
    if exchange_compared and not exchange_state["open_positions"]:
        gaps.append("exchange lookup returned no open positions")

    missing_order_prices = sum(1 for row in open_orders if row["current_reference_price"] is None)
    missing_position_prices = sum(1 for row in positions if row["current_reference_price"] is None)
    if missing_order_prices:
        gaps.append(f"{missing_order_prices} open order(s) missing current/reference price")
    if missing_position_prices:
        gaps.append(f"{missing_position_prices} position(s) missing current/reference price")

    return {
        "configuration": {
            "config_path": config_path,
            "db_path": db_path,
            "exchange_mode": exchange_mode,
            "configured_paper_trading": configured_paper_trading,
            "effective_paper_trading": effective_paper_trading,
        },
        "sources": [
            {
                "name": "sqlite.order_tracking",
                "path": db_path,
                "rows": local_state["table_counts"]["order_tracking"],
                "used_for": "local open-order lifecycle state",
            },
            {
                "name": "sqlite.positions",
                "path": db_path,
                "rows": local_state["table_counts"]["positions"],
                "used_for": "local held shares / entry price / stored mark data",
            },
            {
                "name": "sqlite.idempotency_log",
                "path": db_path,
                "rows": local_state["table_counts"]["idempotency_log"],
                "used_for": "fill-progress hints for tracked orders",
            },
            {
                "name": "exchange.polymarket_clob",
                "path": None,
                "rows": {
                    "open_orders": len(exchange_state["open_orders"]),
                    "open_positions": len(exchange_state["open_positions"]),
                },
                "used_for": (
                    "read-only exchange enrichment"
                    if exchange_compared
                    else "not queried"
                ),
            },
        ],
        "summary": {
            "open_orders": len(open_orders),
            "positions": len(positions),
            "markets_referenced": sorted(
                {
                    row["market_id"]
                    for row in [*open_orders, *positions]
                    if row.get("market_id")
                }
            ),
        },
        "open_orders": open_orders,
        "positions": positions,
        "gaps": sorted(dict.fromkeys(gaps)),
    }


def render_report(report: dict[str, Any]) -> None:
    sections = (
        ("CONFIGURATION", report["configuration"]),
        ("DISCOVERED SOURCES", report["sources"]),
        ("SUMMARY", report["summary"]),
        ("OPEN ORDERS", report["open_orders"]),
        ("POSITIONS", report["positions"]),
        ("GAPS", report["gaps"]),
    )
    for title, payload in sections:
        print(title)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))


async def main_async(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    configured_paper_trading = bool(config.get("trading", {}).get("paper_trading", True))
    effective_paper_trading = resolve_effective_paper_trading(config, args.exchange)
    db_path = resolve_db_path(config, args.db_path)

    ledger = AsyncLedger(db_path=db_path)
    await ledger.initialize()

    client = build_api_client(config, args.exchange)
    try:
        local_state = await collect_local_state(ledger)
        exchange_state, exchange_gaps = await collect_exchange_state(client, effective_paper_trading)
        market_ids = [
            *[row.get("market_id", "") for row in local_state["open_orders"]],
            *[row.get("market_id", "") for row in local_state["open_positions"]],
            *[row.get("market_id", "") for row in exchange_state["open_orders"]],
            *[row.get("market_id", "") for row in exchange_state["open_positions"]],
        ]
        market_cache, market_gaps = await build_market_cache(client, market_ids)
        report = build_report(
            config_path=args.config,
            db_path=db_path,
            configured_paper_trading=configured_paper_trading,
            effective_paper_trading=effective_paper_trading,
            exchange_mode=args.exchange,
            local_state=local_state,
            exchange_state=exchange_state,
            market_cache=market_cache,
            extra_gaps=[*exchange_gaps, *market_gaps],
        )
        render_report(report)
        return 0
    finally:
        if client is not None and hasattr(client, "close"):
            try:
                await _maybe_await(client.close())
            except Exception:
                pass
        await ledger.close()


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()