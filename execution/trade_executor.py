import asyncio
from typing import Dict, Optional
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class TradeExecutor:
    def __init__(self, polymarket_client, bankroll_tracker, kelly_sizer, db):
        self.polymarket = polymarket_client
        self.bankroll = bankroll_tracker
        self.kelly = kelly_sizer
        self.db = db
        self.execution_queue = asyncio.Queue()
    
    async def execute_trade(self, opportunity: Dict) -> bool:
        market_id = opportunity['market_id']
        side = opportunity.get('true_outcome', opportunity.get('side', 'YES'))
        confidence = opportunity['confidence']
        edge = opportunity['edge']
        market_price = opportunity.get('market_price', 0.5)
        
        payout_odds = 1.0 / market_price if market_price > 0 else 2.0
        bet_size = self.kelly.calculate_bet_size(confidence, payout_odds, edge, strategy=opportunity.get('strategy', 'default'))
        
        if bet_size < 0.50:
            logger.debug(f"Bet size too small: ${bet_size:.2f}")
            return False
        
        logger.info(f"💸 EXECUTING TRADE")
        logger.info(f"   Market: {opportunity.get('question', opportunity.get('market_title', 'Unknown'))[:60]}")
        logger.info(f"   Side: {side} | Price: ${market_price:.3f}")
        logger.info(f"   Edge: {edge:.1%} | Confidence: {confidence:.1%}")
        logger.info(f"   Bet Size: ${bet_size:.2f}")
        
        success = await self.polymarket.place_bet(
            market_id=market_id,
            side=side,
            amount=bet_size,
            max_price=market_price * 1.05
        )
        
        if success:
            trade_record = {
                'market_id': market_id,
                'market_title': opportunity.get('question', opportunity.get('market_title')),
                'side': side,
                'entry_price': market_price,
                'bet_size': bet_size,
                'shares': bet_size / market_price if market_price > 0 else 0,
                'status': 'OPEN',
                'strategy': opportunity.get('strategy', 'unknown'),
                'edge': edge,
                'confidence': confidence
            }
            
            trade_id = self.db.log_trade(trade_record)
            trade_record['db_id'] = trade_id
            trade_record['trade_id'] = f"trade_{trade_id}"
            self.bankroll.add_trade(trade_record)
            
            logger.info(f"✅ Trade executed successfully | ID: {trade_id}")
            return True
        else:
            logger.error("❌ Trade execution failed")
            return False
    
    async def process_execution_queue(self):
        while True:
            opportunity = await self.execution_queue.get()
            await self.execute_trade(opportunity)
            await asyncio.sleep(1)
    
    def queue_trade(self, opportunity: Dict):
        self.execution_queue.put_nowait(opportunity)