import asyncio
from decimal import Decimal
from datetime import datetime
from typing import Optional, Dict, List
from data_feeds.news_monitor_v1 import NewsAlert, NewsSeverity
import logging

logger = logging.getLogger(__name__)


class NewsArbitrage:
    """
    Fast-response trading based on news events.
    """

    def __init__(self, polymarket_client, kelly_sizer, config: Dict = None):
        self.poly_client = polymarket_client
        self.kelly_sizer = kelly_sizer
        self.config = config or {}

        self.min_confidence = Decimal(str(self.config.get("min_confidence", 0.70)))
        self.position_size_pct = Decimal(str(self.config.get("position_size_pct", 3.0)))
        self.exit_time_minutes = self.config.get("exit_time_minutes", 30)
        self.max_positions = self.config.get("max_positions", 5)

        self.active_positions: Dict[str, Dict] = {}
        self.executed_trades: List[Dict] = []
        self.stats = {
            "trades_executed": 0,
            "total_profit": Decimal("0"),
            "win_rate": 0.0,
        }

    async def process_news_alert(self, alert: NewsAlert) -> Optional[Dict]:
        """Process news alert and execute trade if conditions met."""
        if len(self.active_positions) >= self.max_positions:
            logger.warning(f"Max positions ({self.max_positions}) reached, skipping")
            return None

        if alert.confidence < float(self.min_confidence):
            logger.debug(f"Low confidence ({alert.confidence:.0%}), skipping")
            return None

        if alert.severity == NewsSeverity.LOW:
            return None

        logger.info(
            f"🔥 Processing news alert: {alert.headline} | "
            f"Direction: {alert.predicted_direction} | "
            f"Confidence: {alert.confidence:.0%}"
        )

        matching_markets = await self._find_matching_markets(alert.affected_markets)

        if not matching_markets:
            logger.info("No matching markets found")
            return None

        results = []
        for market in matching_markets[:3]:
            try:
                result = await self._execute_news_trade(alert, market)
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"Trade execution failed for {market}: {e}")

        return results[0] if results else None

    async def _find_matching_markets(self, keywords: List[str]) -> List[str]:
        """Find Polymarket markets matching news keywords."""
        try:
            if hasattr(self.poly_client, "get_active_markets"):
                markets = await self.poly_client.get_active_markets()
            else:
                markets = await self.poly_client.get_markets(active=True)
            matching = []

            for market in markets:
                title = market.get("question", "").lower()
                for keyword in keywords:
                    if keyword.lower() in title:
                        matching.append(market.get("id"))
                        break

            return [m for m in matching if m][:10]

        except Exception as e:
            logger.error(f"Market search error: {e}")
            return []

    async def _execute_news_trade(self, alert: NewsAlert, market_id: str) -> Optional[Dict]:
        """Execute single news-triggered trade."""
        try:
            orderbook = await self.poly_client.get_market_orderbook_summary(market_id)
            if not orderbook:
                return None

            if alert.predicted_direction == "UP":
                side = "BUY"
                entry_price = orderbook["ask"]
            elif alert.predicted_direction == "DOWN":
                side = "SELL"
                entry_price = orderbook["bid"]
            else:
                logger.warning("Unclear direction, skipping trade")
                return None

            if entry_price <= 0:
                return None

            equity = await self.poly_client.get_account_balance()
            payout_odds = float(Decimal("1") / entry_price)
            edge = float(alert.confidence) - float(entry_price)

            bet_result = self.kelly_sizer.calculate_bet_size(
                bankroll=equity,
                win_probability=float(alert.confidence),
                payout_odds=payout_odds,
                edge=edge,
                sample_size=0
            )

            if hasattr(bet_result, "size"):
                kelly_bet = bet_result.size
            else:
                kelly_bet = Decimal(str(bet_result))

            position_size = min(
                kelly_bet,
                equity * (self.position_size_pct / Decimal("100"))
            )

            if position_size <= 0:
                return None

            order_result = await self.poly_client.place_order(
                market_id=market_id,
                side=side,
                quantity=position_size,
                price=entry_price,
                idempotency_key=f"news_{alert.timestamp.timestamp()}_{market_id}"
            )

            trade_id = order_result.get("order_id")
            self.active_positions[trade_id] = {
                "market_id": market_id,
                "side": side,
                "size": position_size,
                "entry_price": entry_price,
                "entry_time": datetime.utcnow(),
                "alert": alert,
                "order_id": trade_id
            }

            logger.info(
                f"✅ News trade executed: {market_id} | "
                f"Side: {side} | Size: ${position_size:.2f} | "
                f"Price: {entry_price} | Confidence: {alert.confidence:.0%}"
            )

            return {
                "trade_id": trade_id,
                "market_id": market_id,
                "side": side,
                "size": position_size,
                "entry_price": entry_price,
                "alert_headline": alert.headline
            }

        except Exception as e:
            logger.error(f"Trade execution error: {e}")
            return None

    async def check_exit_conditions(self):
        """Check if any positions should be exited."""
        now = datetime.utcnow()
        to_exit = []

        for trade_id, position in list(self.active_positions.items()):
            elapsed = (now - position["entry_time"]).total_seconds() / 60

            if elapsed > self.exit_time_minutes:
                to_exit.append((trade_id, position, "timeout"))

            orderbook = await self.poly_client.get_market_orderbook_summary(position["market_id"])
            if not orderbook:
                continue

            if position["side"] == "BUY":
                current_price = orderbook["bid"]
                pnl = (current_price - position["entry_price"]) / position["entry_price"]
            else:
                current_price = orderbook["ask"]
                pnl = (position["entry_price"] - current_price) / position["entry_price"]

            if pnl > Decimal("0.10"):
                to_exit.append((trade_id, position, "profit_target"))

        for trade_id, position, reason in to_exit:
            try:
                await self._exit_position(trade_id, position, reason)
            except Exception as e:
                logger.error(f"Exit error: {e}")

    async def _exit_position(self, trade_id: str, position: Dict, reason: str):
        """Exit a position."""
        try:
            orderbook = await self.poly_client.get_market_orderbook_summary(position["market_id"])
            if not orderbook:
                return

            exit_side = "SELL" if position["side"] == "BUY" else "BUY"
            exit_price = orderbook["bid"] if exit_side == "SELL" else orderbook["ask"]

            await self.poly_client.place_order(
                market_id=position["market_id"],
                side=exit_side,
                quantity=position["size"],
                price=exit_price,
                idempotency_key=f"exit_{trade_id}"
            )

            if position["side"] == "BUY":
                pnl = (exit_price - position["entry_price"]) * position["size"]
            else:
                pnl = (position["entry_price"] - exit_price) * position["size"]

            self.executed_trades.append({
                "trade_id": trade_id,
                "entry_price": position["entry_price"],
                "exit_price": exit_price,
                "pnl": pnl,
                "reason": reason
            })

            self.stats["trades_executed"] += 1
            self.stats["total_profit"] += pnl

            del self.active_positions[trade_id]

            logger.info(f"✅ Position exited: {trade_id} | P&L: ${pnl:.2f} | Reason: {reason}")

        except Exception as e:
            logger.error(f"Position exit error: {e}")

    def get_stats(self) -> Dict:
        """Get performance statistics."""
        return self.stats
