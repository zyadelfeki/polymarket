#!/usr/bin/env python3
"""
Production Latency Arbitrage Engine

Exploits 15-60 second pricing delays between:
1. Exchange prices (Binance, Coinbase) - real-time
2. Polymarket market prices - lagged

Strategy:
- Monitor CEX price vs Polymarket implied probability
- When gap > threshold (5%+), enter position
- Exit after 30 seconds OR when target hit

Example:
- BTC price on Binance: $95,300 (live)
- Polymarket market "BTC closes above $95,000"
- Market shows 60% YES
- Expected: ~98% YES (outcome already determined)
- Edge: 38%
- Action: Buy YES at 0.60, sell at 0.95 when market catches up

Key: This is NOT prediction. Outcome is already known. Pure arbitrage.
"""

import asyncio
import re
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from decimal import Decimal
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class LatencyOpportunity:
    """Latency arbitrage opportunity"""
    market_id: str
    token_id_yes: str
    token_id_no: str
    question: str
    symbol: str  # 'BTC', 'ETH', 'SOL'
    threshold: Decimal
    exchange_price: Decimal
    market_price_yes: Decimal
    market_price_no: Decimal
    expected_prob: Decimal
    edge: Decimal
    action: str  # 'BUY_YES' or 'BUY_NO'
    confidence: float
    detected_at: datetime

class LatencyArbitrageEngine:
    """
    Production latency arbitrage engine.
    
    Detects CEX price threshold crossings that Polymarket hasn't priced in yet.
    """
    
    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        
        self.min_edge = config.get('min_edge', 0.05)  # 5% minimum edge
        self.max_hold_seconds = config.get('max_hold_seconds', 30)  # 30 second max
        self.target_profit_pct = config.get('target_profit', 0.40)  # Target 40 cents profit
        self.stop_loss_pct = config.get('stop_loss', 0.05)  # -5% stop loss
        
        # Symbols we track
        self.tracked_symbols = ['BTC', 'ETH', 'SOL']
        
        # Cache for recent opportunities (prevent duplicates)
        self.recent_opportunities = []  # Last 100 opportunities
        self.max_cache = 100
        
        logger.info(
            f"LatencyArbitrageEngine initialized: min_edge={self.min_edge*100}%, "
            f"max_hold={self.max_hold_seconds}s"
        )
    
    async def scan_for_opportunities(
        self,
        markets: List[Dict],
        exchange_prices: Dict[str, Decimal],  # {'BTC': Decimal('95300'), ...}
        polymarket_client
    ) -> List[LatencyOpportunity]:
        """
        Scan markets for latency arbitrage opportunities.
        
        Args:
            markets: List of Polymarket markets
            exchange_prices: Current CEX prices {symbol: price}
            polymarket_client: Client to fetch orderbook mid-prices
        
        Returns:
            List of opportunities sorted by edge (highest first)
        """
        opportunities = []
        
        for market in markets:
            question = market.get('question', '').lower()
            condition_id = market.get('condition_id')
            tokens = market.get('tokens', [])
            
            if len(tokens) < 2:
                continue
            
            # Extract symbol and threshold from question
            symbol, threshold = self._extract_symbol_and_threshold(question)
            
            if not symbol or not threshold:
                continue
            
            if symbol not in exchange_prices:
                continue
            
            exchange_price = exchange_prices[symbol]
            
            # Get token IDs
            yes_token = tokens[0].get('token_id')
            no_token = tokens[1].get('token_id')
            
            if not yes_token or not no_token:
                continue
            
            # Get current market prices (REAL mid-prices from orderbook)
            yes_price = await self._get_mid_price(polymarket_client, yes_token)
            no_price = await self._get_mid_price(polymarket_client, no_token)
            
            if yes_price is None or no_price is None:
                continue
            
            # Calculate expected probability based on CEX price
            expected_yes_prob = self._calculate_expected_probability(
                symbol=symbol,
                exchange_price=exchange_price,
                threshold=threshold,
                question=question
            )
            
            # Calculate edge
            current_yes_prob = yes_price
            edge = abs(expected_yes_prob - current_yes_prob)
            
            # Filter by minimum edge
            if edge < self.min_edge:
                continue
            
            # Determine action
            if expected_yes_prob > current_yes_prob:
                action = 'BUY_YES'
                entry_price = yes_price
                target_price = expected_yes_prob
            else:
                action = 'BUY_NO'
                entry_price = no_price
                target_price = Decimal('1.0') - expected_yes_prob
            
            # Calculate confidence (higher edge = higher confidence)
            confidence = min(edge / Decimal('0.30'), Decimal('1.0'))  # 30% edge = 100% confidence
            
            # Create opportunity
            opp = LatencyOpportunity(
                market_id=condition_id,
                token_id_yes=yes_token,
                token_id_no=no_token,
                question=market.get('question'),
                symbol=symbol,
                threshold=threshold,
                exchange_price=exchange_price,
                market_price_yes=yes_price,
                market_price_no=no_price,
                expected_prob=expected_yes_prob,
                edge=edge,
                action=action,
                confidence=float(confidence),
                detected_at=datetime.utcnow()
            )
            
            # Check if duplicate
            if not self._is_duplicate(opp):
                opportunities.append(opp)
                self._add_to_cache(opp)
                
                logger.info(
                    f"Latency arb found: {market.get('question')[:60]} | "
                    f"{symbol}=${exchange_price} vs threshold ${threshold} | "
                    f"Market: {current_yes_prob:.0%} | Expected: {expected_yes_prob:.0%} | "
                    f"Edge: {edge:.1%} | {action}"
                )
        
        # Sort by edge (highest first)
        return sorted(opportunities, key=lambda x: x.edge, reverse=True)
    
    def _extract_symbol_and_threshold(self, question: str) -> Tuple[Optional[str], Optional[Decimal]]:
        """
        Extract crypto symbol and price threshold from market question.
        
        Examples:
        - "Bitcoin closes above $95,000" -> ('BTC', Decimal('95000'))
        - "ETH > 3,000 USDT" -> ('ETH', Decimal('3000'))
        - "SOL price below $200" -> ('SOL', Decimal('200'))
        
        Returns:
            (symbol, threshold) or (None, None) if not found
        """
        # Detect symbol
        symbol = None
        for sym in self.tracked_symbols:
            # Check for full name or abbreviation
            patterns = [
                rf'\b{sym}\b',  # Exact match (e.g., "BTC")
                rf'\bbitcoin\b' if sym == 'BTC' else None,
                rf'\bethereum\b' if sym == 'ETH' else None,
                rf'\bsolana\b' if sym == 'SOL' else None
            ]
            
            for pattern in patterns:
                if pattern and re.search(pattern, question, re.IGNORECASE):
                    symbol = sym
                    break
            
            if symbol:
                break
        
        if not symbol:
            return None, None
        
        # Extract threshold
        threshold_patterns = [
            r'(?:above|over|greater than|>|>=)\s*\$?([\d,]+)',  # "above $95,000"
            r'(?:below|under|less than|<|<=)\s*\$?([\d,]+)',    # "below $95,000"
            r'([\d,]+)\s*(?:usdt|usd|dollars?)',                # "3,000 USDT"
            r'\$([\d,]+)'                                        # "$95,000"
        ]
        
        for pattern in threshold_patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                try:
                    threshold_str = match.group(1).replace(',', '')
                    threshold = Decimal(threshold_str)
                    return symbol, threshold
                except (ValueError, IndexError):
                    continue
        
        return symbol, None
    
    def _calculate_expected_probability(
        self,
        symbol: str,
        exchange_price: Decimal,
        threshold: Decimal,
        question: str
    ) -> Decimal:
        """
        Calculate what the probability SHOULD be based on current exchange price.
        
        If CEX price already crossed threshold:
        - For "above" questions: ~95-98% YES
        - For "below" questions: ~2-5% YES
        
        Returns:
            Expected probability (0-1)
        """
        # Determine direction from question
        above_keywords = ['above', 'over', 'greater than', '>', '>=']
        below_keywords = ['below', 'under', 'less than', '<', '<=']
        
        is_above_question = any(kw in question.lower() for kw in above_keywords)
        is_below_question = any(kw in question.lower() for kw in below_keywords)
        
        if is_above_question:
            if exchange_price > threshold:
                return Decimal('0.98')  # Already happened
            else:
                return Decimal('0.02')  # Still needs to happen
        
        elif is_below_question:
            if exchange_price < threshold:
                return Decimal('0.98')  # Already happened
            else:
                return Decimal('0.02')  # Still needs to happen
        
        else:
            # Can't determine direction - neutral
            logger.warning(f"Could not determine direction for: {question}")
            return Decimal('0.50')
    
    async def _get_mid_price(self, client, token_id: str) -> Optional[Decimal]:
        """
        Get mid-price from orderbook.
        
        Mid-price = (best_bid + best_ask) / 2
        
        Returns:
            Mid-price as Decimal or None if orderbook unavailable
        """
        try:
            orderbook = await client.get_market_orderbook(token_id)
            
            if not orderbook:
                return None
            
            bids = orderbook.get('bids', [])
            asks = orderbook.get('asks', [])
            
            if not bids or not asks:
                return None
            
            # Get best bid and ask
            best_bid = Decimal(str(bids[0]['price']))
            best_ask = Decimal(str(asks[0]['price']))
            
            mid = (best_bid + best_ask) / 2
            
            return mid
        
        except Exception as e:
            logger.error(f"Error fetching mid-price for {token_id}: {e}")
            return None
    
    def _is_duplicate(self, opp: LatencyOpportunity) -> bool:
        """
        Check if opportunity is duplicate of recent one.
        
        Prevents trading same market multiple times in short window.
        """
        for recent in self.recent_opportunities:
            if recent.market_id == opp.market_id:
                time_diff = (opp.detected_at - recent.detected_at).total_seconds()
                if time_diff < 60:  # 1 minute cooldown
                    return True
        
        return False
    
    def _add_to_cache(self, opp: LatencyOpportunity):
        """Add opportunity to recent cache"""
        self.recent_opportunities.append(opp)
        if len(self.recent_opportunities) > self.max_cache:
            self.recent_opportunities.pop(0)
    
    def calculate_exit_price(self, opp: LatencyOpportunity, entry_price: Decimal) -> Decimal:
        """
        Calculate target exit price.
        
        Target: Entry + target_profit_pct (default 40 cents)
        Capped at expected probability
        """
        target = entry_price + Decimal(str(self.target_profit_pct))
        
        # Cap at expected probability (can't sell YES above 1.0)
        if opp.action == 'BUY_YES':
            target = min(target, opp.expected_prob)
        else:
            target = min(target, Decimal('1.0') - opp.expected_prob)
        
        return target
    
    def should_stop_loss(self, current_price: Decimal, entry_price: Decimal) -> bool:
        """
        Check if stop loss triggered.
        
        Stop loss: -5% from entry
        """
        loss_pct = (current_price - entry_price) / entry_price
        return loss_pct < -Decimal(str(self.stop_loss_pct))