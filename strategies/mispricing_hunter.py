from decimal import Decimal, ROUND_DOWN, getcontext
from typing import Optional, Dict, List
import json
import time
from pathlib import Path

try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

getcontext().prec = 18

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)


class MispricingHunter:
    """
    Exploit latency between Binance spot and Polymarket derivatives.
    """

    def __init__(self, binance_feed, min_edge: Decimal = Decimal("0.05")):
        self.binance_feed = binance_feed
        self.min_edge = min_edge
        self.fee_rate = Decimal("0.02")
        self.current_signal: Optional[str] = None

    async def scan_for_mispricings(self, markets: List[Dict]) -> List[Dict]:
        opportunities: List[Dict] = []

        btc_price = None
        if hasattr(self.binance_feed, "get_price"):
            result = self.binance_feed.get_price("BTC")
            if hasattr(result, "__await__"):
                result = await result
            btc_price = result

        if not btc_price:
            logger.warning("no_binance_price")
            return []

        for market in markets:
            opp = await self._analyze_market(market, Decimal(str(btc_price)))
            if opp:
                opportunities.append(opp)

        return opportunities

    async def _analyze_market(self, market: Dict, binance_price: Decimal) -> Optional[Dict]:
        strike_price = self._extract_strike(market.get("question", ""))
        if not strike_price:
            return None

        yes_price = Decimal(str(market["yes_price"]))
        no_price = Decimal("1.0") - yes_price

        if binance_price > strike_price:
            true_prob = Decimal("0.95")
            edge_gross = true_prob - yes_price
            edge_net = edge_gross - self.fee_rate

            if edge_net >= self.min_edge:
                logger.info(
                    "mispricing_opportunity",
                    market=market.get("question", "")[:50],
                    strike=str(strike_price),
                    btc=str(binance_price),
                    yes=str(yes_price),
                    edge=str(edge_net),
                )
                self.current_signal = "UP"
                return {
                    "market_id": market["id"],
                    "question": market.get("question"),
                    "signal": "BUY_YES",
                    "token_id": market["yes_token_id"],
                    "entry_price": yes_price,
                    "edge_net": edge_net,
                    "confidence": true_prob,
                    "reason": f"BTC {binance_price} > Strike {strike_price}",
                }

        if binance_price < strike_price:
            true_prob = Decimal("0.95")
            edge_gross = true_prob - no_price
            edge_net = edge_gross - self.fee_rate

            if edge_net >= self.min_edge:
                logger.info(
                    "mispricing_opportunity",
                    market=market.get("question", "")[:50],
                    strike=str(strike_price),
                    btc=str(binance_price),
                    no=str(no_price),
                    edge=str(edge_net),
                )
                self.current_signal = "DOWN"
                return {
                    "market_id": market["id"],
                    "question": market.get("question"),
                    "signal": "BUY_NO",
                    "token_id": market["no_token_id"],
                    "entry_price": no_price,
                    "edge_net": edge_net,
                    "confidence": true_prob,
                    "reason": f"BTC {binance_price} < Strike {strike_price}",
                }

        return None

    def _extract_strike(self, question: str) -> Optional[Decimal]:
        import re

        patterns = [
            r"\$([0-9,]+)",
            r"(\d{4,6})",
        ]

        for pattern in patterns:
            match = re.search(pattern, question)
            if match:
                price_str = match.group(1).replace(",", "")
                return Decimal(price_str)

        return None

    def apply_charlie_boost(self, base_confidence: Decimal) -> Decimal:
        try:
            intel_file = Path("./shared_data/charlie_intelligence.json")
            with open(intel_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            if time.time() - data.get("timestamp", 0) > 30:
                logger.debug("charlie_intel_stale")
                return base_confidence

            boosted = base_confidence

            if data.get("lstm", {}).get("prediction") == self.current_signal:
                boost = Decimal(str(data.get("lstm", {}).get("confidence", 0))) * Decimal("0.10")
                boosted += boost
                logger.info("lstm_alignment_bonus", bonus=str(boost))

            whale_flow = Decimal(str(data.get("whale_flow", 0)))
            if (self.current_signal == "UP" and whale_flow > 0) or (
                self.current_signal == "DOWN" and whale_flow < 0
            ):
                boosted += Decimal("0.05")
                logger.info("whale_confirmation_bonus", bonus="0.05")

            mev_vol = Decimal(str(data.get("mev_volatility", 0)))
            if mev_vol > Decimal("0.7"):
                penalty = Decimal("0.05")
                boosted -= penalty
                logger.warning("mev_volatility_penalty", penalty=str(penalty))

            return max(Decimal("0"), min(boosted, Decimal("1")))

        except Exception as exc:
            logger.debug("charlie_boost_unavailable", error=str(exc))
            return base_confidence
