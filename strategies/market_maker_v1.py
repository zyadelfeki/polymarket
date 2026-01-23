import asyncio
from decimal import Decimal
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import logging
import uuid

logger = logging.getLogger(__name__)


@dataclass
class Quote:
    """Active market quote."""
    market_id: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    quote_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def spread_pct(self) -> Decimal:
        """Calculate spread as percentage."""
        mid = (self.bid + self.ask) / 2
        return ((self.ask - self.bid) / mid) * 100


class MarketMaker:
    """
    Market making strategy for Polymarket.
    """

    def __init__(self, polymarket_client, config: Dict = None):
        self.poly_client = polymarket_client
        self.config = config or {}

        self.max_inventory_pct = Decimal(str(self.config.get("max_inventory_pct", 10.0)))
        self.target_spread_pct = Decimal(str(self.config.get("target_spread_pct", 4.0)))
        self.min_spread_pct = Decimal(str(self.config.get("min_spread_pct", 2.0)))
        self.max_spread_pct = Decimal(str(self.config.get("max_spread_pct", 8.0)))
        self.quote_duration_seconds = self.config.get("quote_duration_seconds", 600)
        self.rebalance_interval_seconds = self.config.get("rebalance_interval_seconds", 300)
        self.max_markets = self.config.get("max_markets", 5)

        self.active_quotes: Dict[str, Quote] = {}
        self.market_inventory: Dict[str, Decimal] = {}
        self.positions: Dict[str, Dict] = {}

        self.stats = {
            "quotes_placed": 0,
            "quotes_filled": 0,
            "total_spread_captured": Decimal("0"),
            "daily_profit": Decimal("0"),
            "inventory_value": Decimal("0"),
        }

        self.last_rebalance = datetime.utcnow()
        self.selected_markets: List[str] = []

    async def select_markets(self) -> List[str]:
        """Select best markets for market making."""
        try:
            if hasattr(self.poly_client, "get_active_markets"):
                markets = await self.poly_client.get_active_markets()
            else:
                markets = await self.poly_client.get_markets(active=True)

            scored_markets = []

            for market in markets:
                market_id = market.get("id") or market.get("market_id")
                if not market_id:
                    continue

                try:
                    orderbook = await self.poly_client.get_market_orderbook_summary(market_id)
                    if not orderbook:
                        continue

                    total_volume = orderbook["bid_volume"] + orderbook["ask_volume"]
                    if total_volume < Decimal("100"):
                        continue

                    if orderbook["bid"] > 0:
                        spread = (orderbook["ask"] - orderbook["bid"]) / orderbook["bid"]
                        spread_pct = float(spread) * 100
                    else:
                        spread_pct = 10.0

                    if spread_pct < 1.0 or spread_pct > 10.0:
                        continue

                    balance = min(orderbook["bid_volume"], orderbook["ask_volume"]) / max(orderbook["bid_volume"], orderbook["ask_volume"])

                    volume_score = min(1.0, float(total_volume) / 1000)
                    spread_score = 1.0 - (abs(spread_pct - 4.0) / 10.0)
                    balance_score = float(balance)

                    composite_score = (
                        volume_score * 0.4 +
                        spread_score * 0.35 +
                        balance_score * 0.25
                    )

                    scored_markets.append((market_id, composite_score))

                except Exception as e:
                    logger.debug(f"Market {market_id} analysis error: {e}")
                    continue

            scored_markets.sort(key=lambda x: x[1], reverse=True)
            self.selected_markets = [m[0] for m in scored_markets[:self.max_markets]]

            logger.info(f"📊 Selected {len(self.selected_markets)} markets for MM")
            return self.selected_markets

        except Exception as e:
            logger.error(f"Market selection error: {e}")
            return []

    async def calculate_spreads(self, orderbook: Dict) -> Tuple[Decimal, Decimal]:
        """Calculate optimal bid/ask spreads based on market conditions."""
        try:
            bid_spread = self.target_spread_pct / 2
            ask_spread = self.target_spread_pct / 2

            volatility = await self._estimate_volatility(orderbook["market_id"])
            volatility_factor = 0.8 + (volatility * 0.4)

            bid_spread = bid_spread * Decimal(str(volatility_factor))
            ask_spread = ask_spread * Decimal(str(volatility_factor))

            inventory = self.market_inventory.get(orderbook["market_id"], Decimal("0"))
            max_inventory = self.market_inventory.get(orderbook["market_id"], Decimal("100")) * Decimal("0.3")

            if inventory > max_inventory:
                ask_spread = ask_spread * Decimal("1.5")
            elif inventory < -max_inventory:
                bid_spread = bid_spread * Decimal("1.5")

            bid_spread = max(self.min_spread_pct / 2, min(self.max_spread_pct / 2, bid_spread))
            ask_spread = max(self.min_spread_pct / 2, min(self.max_spread_pct / 2, ask_spread))

            return bid_spread, ask_spread

        except Exception as e:
            logger.warning(f"Spread calculation error: {e}")
            return self.target_spread_pct / 2, self.target_spread_pct / 2

    async def _estimate_volatility(self, market_id: str) -> float:
        """Estimate market volatility (0.0 to 1.0)."""
        try:
            orderbook = await self.poly_client.get_market_orderbook_summary(market_id)
            if not orderbook:
                return 0.5
            mid_price = (orderbook["bid"] + orderbook["ask"]) / 2

            recent_avg = Decimal("0.50")
            deviation = abs(mid_price - recent_avg) / recent_avg

            volatility = min(1.0, float(deviation) * 2)
            return volatility
        except Exception:
            return 0.5

    async def place_quotes(self, market_id: str) -> Optional[Quote]:
        """Place bid and ask quotes in market."""
        try:
            orderbook = await self.poly_client.get_market_orderbook_summary(market_id)
            if not orderbook:
                return None

            mid_price = (orderbook["bid"] + orderbook["ask"]) / 2

            bid_spread, ask_spread = await self.calculate_spreads(orderbook)

            bid = mid_price - (mid_price * bid_spread / 100)
            ask = mid_price + (mid_price * ask_spread / 100)

            equity = await self.poly_client.get_account_balance()
            quote_size = equity * Decimal("0.01")

            bid_order = await self.poly_client.place_order(
                market_id=market_id,
                side="BUY",
                quantity=quote_size,
                price=bid,
                idempotency_key=f"mm_bid_{market_id}_{datetime.utcnow().timestamp()}"
            )

            ask_order = await self.poly_client.place_order(
                market_id=market_id,
                side="SELL",
                quantity=quote_size,
                price=ask,
                idempotency_key=f"mm_ask_{market_id}_{datetime.utcnow().timestamp()}"
            )

            quote = Quote(
                market_id=market_id,
                bid=bid,
                ask=ask,
                bid_size=quote_size,
                ask_size=quote_size
            )

            self.active_quotes[quote.quote_id] = quote
            self.stats["quotes_placed"] += 1

            logger.info(
                f"📍 Quote placed for {market_id}: "
                f"Bid: {bid:.4f}, Ask: {ask:.4f}, Spread: {quote.spread_pct:.2f}%"
            )

            return quote

        except Exception as e:
            logger.error(f"Quote placement error: {e}")
            return None

    async def check_fills(self) -> List[Dict]:
        """Check which quotes were filled."""
        filled: List[Dict] = []

        try:
            positions = await self.poly_client.get_positions()

            for position in positions:
                market_id = position.get("market_id") or position.get("marketId")
                if not market_id:
                    continue
                size = Decimal(str(position.get("quantity", 0)))

                if market_id in self.market_inventory:
                    old_inventory = self.market_inventory[market_id]
                    if old_inventory != size:
                        side = "BUY" if size > old_inventory else "SELL"
                        filled.append({
                            "market_id": market_id,
                            "side": side,
                            "size": abs(size - old_inventory),
                            "new_inventory": size
                        })

                        self.market_inventory[market_id] = size
                        self.stats["quotes_filled"] += 1

                        logger.info(f"✅ Quote filled: {market_id} | Side: {side} | Size: {size}")

            return filled

        except Exception as e:
            logger.error(f"Fill check error: {e}")
            return []

    async def rebalance(self):
        """Rebalance inventory and refresh quotes."""
        now = datetime.utcnow()
        if (now - self.last_rebalance).total_seconds() < self.rebalance_interval_seconds:
            return

        logger.info("♻️ Rebalancing market maker...")

        try:
            await self.check_fills()

            for quote_id in list(self.active_quotes.keys()):
                quote = self.active_quotes[quote_id]
                age = (now - quote.created_at).total_seconds()

                if age > self.quote_duration_seconds:
                    logger.debug(f"Cancelling stale quote: {quote_id}")
                    del self.active_quotes[quote_id]

            for market_id in self.selected_markets:
                try:
                    await self.place_quotes(market_id)
                except Exception as e:
                    logger.warning(f"Quote refresh error for {market_id}: {e}")

            self.last_rebalance = now

            logger.info(
                f"📈 MM Stats: "
                f"Quotes: {self.stats['quotes_placed']}, "
                f"Filled: {self.stats['quotes_filled']}, "
                f"Spread captured: ${self.stats['total_spread_captured']:.2f}"
            )

        except Exception as e:
            logger.error(f"Rebalance error: {e}")

    async def run_market_making_loop(self):
        """Main market making loop."""
        await self.select_markets()

        while True:
            try:
                await self.rebalance()
                await asyncio.sleep(30)

            except KeyboardInterrupt:
                logger.info("Market making stopped")
                break
            except Exception as e:
                logger.error(f"MM loop error: {e}")
                await asyncio.sleep(10)

    def get_stats(self) -> Dict:
        """Get market making statistics."""
        return {
            **self.stats,
            "active_quotes": len(self.active_quotes),
            "tracked_markets": len(self.selected_markets),
            "average_spread": self.target_spread_pct,
        }
