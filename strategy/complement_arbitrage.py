#!/usr/bin/env python3
"""
Complement Arbitrage - NEAR-ZERO RISK

Strategy:
Buy YES + NO shares when total cost < $1.00

Example:
- YES trading at $0.45
- NO trading at $0.52
- Total cost: $0.97
- When market resolves, one side pays $1.00
- Guaranteed profit: $0.03 per share (3.1% return)

This is PURE ARBITRAGE. No directional risk.

Profitability: Near-guaranteed if executed properly
Risk: Execution risk only (order fills, fees)
"""

import asyncio
import logging
from typing import Dict, List, Optional
from decimal import Decimal
from datetime import datetime

logger = logging.getLogger(__name__)

class ComplementArbitrageEngine:
    """
    Complement Arbitrage: Buy YES + NO when total < $1
    """
    
    def __init__(self):
        self.MIN_PROFIT_PCT = 0.015  # 1.5% minimum profit
        self.MIN_LIQUIDITY = 50  # Minimum liquidity per side
        self.MAX_POSITION_SIZE = 100  # Max $100 per arb
        
        self.executed_arbs = []
    
    async def scan_for_opportunities(self, client, markets: List[Dict]) -> List[Dict]:
        """
        Scan all binary markets for complement arbitrage opportunities
        
        Looks for markets where YES + NO < $1.00
        """
        opportunities = []
        
        for market in markets:
            tokens = market.get('tokens', [])
            
            # Must be binary market (YES/NO)
            if len(tokens) != 2:
                continue
            
            yes_token = tokens[0]
            no_token = tokens[1]
            
            yes_token_id = yes_token.get('token_id')
            no_token_id = no_token.get('token_id')
            
            if not yes_token_id or not no_token_id:
                continue
            
            # Get best ask prices (what we'd pay to buy)
            yes_prices = client.get_best_bid_ask(yes_token_id)
            no_prices = client.get_best_bid_ask(no_token_id)
            
            yes_ask = yes_prices['ask']
            no_ask = no_prices['ask']
            
            # Total cost to buy both sides
            total_cost = yes_ask + no_ask
            
            # Profit when market resolves (one side pays $1)
            profit = 1.0 - total_cost
            profit_pct = profit / total_cost if total_cost > 0 else 0
            
            # Check if profitable
            if profit_pct > self.MIN_PROFIT_PCT:
                # Check liquidity
                yes_depth = client.calculate_orderbook_depth(yes_token_id, levels=3)
                no_depth = client.calculate_orderbook_depth(no_token_id, levels=3)
                
                if yes_depth['ask_depth'] < self.MIN_LIQUIDITY or no_depth['ask_depth'] < self.MIN_LIQUIDITY:
                    continue
                
                opportunity = {
                    'type': 'complement_arbitrage',
                    'market_id': market.get('condition_id'),
                    'question': market.get('question'),
                    'yes_token_id': yes_token_id,
                    'no_token_id': no_token_id,
                    'yes_ask': yes_ask,
                    'no_ask': no_ask,
                    'total_cost': total_cost,
                    'guaranteed_payout': 1.0,
                    'profit': profit,
                    'profit_pct': profit_pct,
                    'yes_liquidity': yes_depth['ask_depth'],
                    'no_liquidity': no_depth['ask_depth'],
                    'confidence': 1.0,  # Risk-free
                    'timestamp': datetime.utcnow()
                }
                
                opportunities.append(opportunity)
                
                logger.info(
                    f"Complement Arb Found: {market.get('question')[:50]}... | "
                    f"YES=${yes_ask:.3f} + NO=${no_ask:.3f} = ${total_cost:.3f} | "
                    f"Profit: ${profit:.4f} ({profit_pct:.2%})"
                )
        
        return sorted(opportunities, key=lambda x: x['profit_pct'], reverse=True)
    
    async def execute_complement_arb(self,
                                     client,
                                     opportunity: Dict,
                                     position_size: float) -> Optional[Dict]:
        """
        Execute complement arbitrage by buying both YES and NO
        
        Steps:
        1. Buy YES shares at ask price
        2. Buy NO shares at ask price
        3. Hold until market resolves
        4. Collect $1.00 from winning side
        """
        entry_time = datetime.utcnow()
        
        yes_token_id = opportunity['yes_token_id']
        no_token_id = opportunity['no_token_id']
        yes_ask = opportunity['yes_ask']
        no_ask = opportunity['no_ask']
        total_cost = opportunity['total_cost']
        
        # Calculate shares to buy (equal on both sides)
        shares = min(
            position_size / total_cost,
            self.MAX_POSITION_SIZE / total_cost
        )
        
        amount_yes = shares * yes_ask
        amount_no = shares * no_ask
        
        logger.info(
            f"Executing Complement Arb: {opportunity['question'][:40]}... | "
            f"Buying {shares:.2f} YES @ ${yes_ask:.3f} + {shares:.2f} NO @ ${no_ask:.3f} | "
            f"Total: ${amount_yes + amount_no:.2f}"
        )
        
        # Buy YES
        yes_order = client.market_buy(yes_token_id, amount_yes)
        if not yes_order or not yes_order.get('success'):
            logger.error("Failed to buy YES shares")
            return None
        
        # Buy NO
        no_order = client.market_buy(no_token_id, amount_no)
        if not no_order or not no_order.get('success'):
            logger.error("Failed to buy NO shares - attempting to unwind YES position")
            await self._unwind_position(client, yes_token_id, shares)
            return None
        
        # Calculate actual costs (may differ due to slippage)
        actual_yes_cost = amount_yes  # Simplified
        actual_no_cost = amount_no    # Simplified
        actual_total_cost = actual_yes_cost + actual_no_cost
        
        # Expected profit
        expected_payout = shares * 1.0  # One side pays $1 per share
        expected_profit = expected_payout - actual_total_cost
        expected_roi = expected_profit / actual_total_cost if actual_total_cost > 0 else 0
        
        trade = {
            'type': 'complement_arbitrage',
            'market_id': opportunity['market_id'],
            'question': opportunity['question'],
            'shares': shares,
            'yes_cost': actual_yes_cost,
            'no_cost': actual_no_cost,
            'total_cost': actual_total_cost,
            'expected_payout': expected_payout,
            'expected_profit': expected_profit,
            'expected_roi': expected_roi,
            'yes_order_id': yes_order.get('order_id'),
            'no_order_id': no_order.get('order_id'),
            'entry_time': entry_time,
            'status': 'open',
            'exit_time': None,
            'realized_profit': None
        }
        
        self.executed_arbs.append(trade)
        
        logger.info(
            f"✅ Complement Arb Executed | "
            f"Cost: ${actual_total_cost:.2f} | "
            f"Expected Profit: ${expected_profit:.2f} ({expected_roi:+.2%})"
        )
        
        return trade

    async def _unwind_position(self, client, token_id: str, shares: float) -> None:
        """Best-effort unwind for a single token position."""
        if not token_id or not shares:
            return
        try:
            result = None
            if hasattr(client, "market_sell"):
                result = client.market_sell(token_id, shares)
            elif hasattr(client, "place_order"):
                result = client.place_order(token_id=token_id, side="SELL", amount=shares, price=1.0)
            if asyncio.iscoroutine(result):
                result = await result
            if not result or not result.get("success"):
                logger.error(f"Unwind failed for token {token_id}")
        except Exception as e:
            logger.error(f"Unwind error for token {token_id}: {e}")
    
    def get_open_positions(self) -> List[Dict]:
        """
        Return all open complement arbitrage positions
        """
        return [arb for arb in self.executed_arbs if arb['status'] == 'open']
    
    async def check_resolutions(self, client) -> List[Dict]:
        """
        Check if any open positions have resolved and calculate realized P&L
        
        Returns list of resolved trades with realized profits
        """
        resolved = []
        
        for arb in self.executed_arbs:
            if arb['status'] != 'open':
                continue
            
            # Check if market resolved (would query Polymarket API)
            # For now, placeholder
            pass
        
        return resolved


# Example usage
if __name__ == '__main__':
    engine = ComplementArbitrageEngine()
    print("Complement Arbitrage Engine initialized")
    print(f"Min profit threshold: {engine.MIN_PROFIT_PCT:.1%}")
    print(f"Max position size: ${engine.MAX_POSITION_SIZE}")
