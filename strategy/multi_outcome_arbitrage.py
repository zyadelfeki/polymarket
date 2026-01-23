#!/usr/bin/env python3
"""
Multi-Outcome Arbitrage - NEAR-ZERO RISK

Strategy:
Buy ALL outcomes in a multi-outcome market when total cost < $1.00

Example Market: "Who will win the election?"
- Candidate A: $0.25
- Candidate B: $0.30
- Candidate C: $0.22
- Candidate D: $0.20
- Total: $0.97

When market resolves, one candidate pays $1.00
Guaranteed profit: $0.03 per "set" (3.1% return)

This is PURE ARBITRAGE. Zero directional risk.

Profitability: Near-guaranteed
Risk: Execution only (need to buy ALL outcomes)
"""

import asyncio
import logging
from typing import Dict, List, Optional
from decimal import Decimal
from datetime import datetime

logger = logging.getLogger(__name__)

class MultiOutcomeArbitrageEngine:
    """
    Multi-Outcome Arbitrage: Buy all outcomes when total < $1
    """
    
    def __init__(self):
        self.MIN_PROFIT_PCT = 0.02  # 2% minimum profit
        self.MIN_LIQUIDITY_PER_OUTCOME = 30  # Min liquidity per outcome
        self.MAX_POSITION_SIZE = 100  # Max $100 per arb
        
        self.executed_arbs = []
    
    async def scan_for_opportunities(self, client, markets: List[Dict]) -> List[Dict]:
        """
        Scan all multi-outcome markets for arbitrage
        
        Looks for markets where sum(all outcome prices) < $1.00
        """
        opportunities = []
        
        for market in markets:
            tokens = market.get('tokens', [])
            
            # Must be multi-outcome (3+ outcomes)
            if len(tokens) < 3:
                continue
            
            # Get ask prices for all outcomes
            outcome_prices = []
            outcome_tokens = []
            
            all_liquid = True
            
            for token in tokens:
                token_id = token.get('token_id')
                if not token_id:
                    all_liquid = False
                    break
                
                prices = client.get_best_bid_ask(token_id)
                ask = prices['ask']
                
                # Check liquidity
                depth = client.calculate_orderbook_depth(token_id, levels=3)
                if depth['ask_depth'] < self.MIN_LIQUIDITY_PER_OUTCOME:
                    all_liquid = False
                    break
                
                outcome_prices.append(ask)
                outcome_tokens.append({
                    'token_id': token_id,
                    'outcome': token.get('outcome', f"Outcome {len(outcome_tokens) + 1}"),
                    'ask': ask,
                    'liquidity': depth['ask_depth']
                })
            
            if not all_liquid:
                continue
            
            # Calculate total cost
            total_cost = sum(outcome_prices)
            
            # Profit when market resolves (one outcome pays $1)
            profit = 1.0 - total_cost
            profit_pct = profit / total_cost if total_cost > 0 else 0
            
            # Check if profitable
            if profit_pct > self.MIN_PROFIT_PCT:
                opportunity = {
                    'type': 'multi_outcome_arbitrage',
                    'market_id': market.get('condition_id'),
                    'question': market.get('question'),
                    'outcomes': outcome_tokens,
                    'total_cost': total_cost,
                    'guaranteed_payout': 1.0,
                    'profit': profit,
                    'profit_pct': profit_pct,
                    'num_outcomes': len(outcome_tokens),
                    'confidence': 1.0,  # Risk-free
                    'timestamp': datetime.utcnow()
                }
                
                opportunities.append(opportunity)
                
                outcomes_str = ', '.join([f"{o['outcome'][:15]}=${o['ask']:.3f}" for o in outcome_tokens])
                logger.info(
                    f"Multi-Outcome Arb Found: {market.get('question')[:40]}... | "
                    f"{len(outcome_tokens)} outcomes | Total: ${total_cost:.3f} | "
                    f"Profit: ${profit:.4f} ({profit_pct:.2%})"
                )
        
        return sorted(opportunities, key=lambda x: x['profit_pct'], reverse=True)
    
    async def execute_multi_outcome_arb(self,
                                        client,
                                        opportunity: Dict,
                                        position_size: float) -> Optional[Dict]:
        """
        Execute multi-outcome arbitrage by buying all outcomes
        
        Steps:
        1. Buy equal shares of ALL outcomes
        2. Hold until market resolves
        3. Collect $1.00 from winning outcome
        """
        entry_time = datetime.utcnow()
        
        outcomes = opportunity['outcomes']
        total_cost = opportunity['total_cost']
        
        # Calculate shares to buy
        shares = min(
            position_size / total_cost,
            self.MAX_POSITION_SIZE / total_cost
        )
        
        total_spent = 0.0
        orders = []
        
        logger.info(
            f"Executing Multi-Outcome Arb: {opportunity['question'][:40]}... | "
            f"Buying {shares:.2f} shares of each of {len(outcomes)} outcomes | "
            f"Total budget: ${shares * total_cost:.2f}"
        )
        
        # Buy all outcomes
        for outcome in outcomes:
            token_id = outcome['token_id']
            ask = outcome['ask']
            amount = shares * ask
            
            order = client.market_buy(token_id, amount)
            
            if not order or not order.get('success'):
                logger.error(f"Failed to buy {outcome['outcome']} - aborting arb")
                await self._unwind_orders(client, orders)
                return None
            
            orders.append({
                'outcome': outcome['outcome'],
                'token_id': token_id,
                'shares': shares,
                'cost': amount,
                'order_id': order.get('order_id')
            })
            
            total_spent += amount
        
        # Calculate expected profit
        expected_payout = shares * 1.0  # Winning outcome pays $1 per share
        expected_profit = expected_payout - total_spent
        expected_roi = expected_profit / total_spent if total_spent > 0 else 0
        
        trade = {
            'type': 'multi_outcome_arbitrage',
            'market_id': opportunity['market_id'],
            'question': opportunity['question'],
            'shares': shares,
            'orders': orders,
            'total_cost': total_spent,
            'expected_payout': expected_payout,
            'expected_profit': expected_profit,
            'expected_roi': expected_roi,
            'entry_time': entry_time,
            'status': 'open',
            'exit_time': None,
            'realized_profit': None
        }
        
        self.executed_arbs.append(trade)
        
        logger.info(
            f"✅ Multi-Outcome Arb Executed | "
            f"{len(orders)} outcomes bought | "
            f"Cost: ${total_spent:.2f} | "
            f"Expected Profit: ${expected_profit:.2f} ({expected_roi:+.2%})"
        )
        
        return trade

    async def _unwind_orders(self, client, orders: List[Dict]) -> None:
        """Best-effort unwind for already placed orders."""
        if not orders:
            return
        for order in orders:
            token_id = order.get('token_id')
            shares = order.get('shares')
            if not token_id or not shares:
                continue
            try:
                result = None
                if hasattr(client, "market_sell"):
                    result = client.market_sell(token_id, shares)
                elif hasattr(client, "place_order"):
                    result = client.place_order(token_id=token_id, side="SELL", amount=shares, price=1.0)
                if asyncio.iscoroutine(result):
                    result = await result
                if not result or not result.get("success"):
                    logger.error(
                        "unwind_failed",
                        extra={"token_id": token_id, "shares": shares}
                    )
            except Exception as e:
                logger.error(f"Unwind error for token {token_id}: {e}")


# Example usage
if __name__ == '__main__':
    engine = MultiOutcomeArbitrageEngine()
    print("Multi-Outcome Arbitrage Engine initialized")
    print(f"Min profit threshold: {engine.MIN_PROFIT_PCT:.1%}")
    print(f"Max position size: ${engine.MAX_POSITION_SIZE}")
