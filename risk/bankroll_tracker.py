from decimal import Decimal
from typing import Dict, List
import logging
from config.settings import settings
from utils.db import Database

logger = logging.getLogger(__name__)

class BankrollTracker:
    def __init__(self, db: Database):
        self.db = db
        self.initial_capital = settings.INITIAL_CAPITAL
        self.current_capital = self.initial_capital
        self.peak_capital = self.initial_capital
        self.open_positions = []
        self.trade_history = []
        self.consecutive_wins = 0
        self.consecutive_losses = 0
    
    def get_current_capital(self) -> float:
        return float(self.current_capital)
    
    def get_available_capital(self) -> float:
        locked_capital = sum(pos.get('bet_size', 0) for pos in self.open_positions)
        available = float(self.current_capital) - locked_capital
        reserve = float(self.current_capital) * (settings.CASH_RESERVE_PCT / 100)
        return max(available - reserve, 0)
    
    def add_trade(self, trade: Dict):
        self.open_positions.append(trade)
        logger.info(f"📊 Position opened: ${trade.get('bet_size', 0):.2f} | Open: {len(self.open_positions)}")
    
    def close_trade(self, trade_id: str, exit_price: float, exit_reason: str):
        for i, pos in enumerate(self.open_positions):
            if pos.get('trade_id') == trade_id:
                entry_price = pos['entry_price']
                bet_size = pos['bet_size']
                shares = bet_size / entry_price if entry_price > 0 else 0
                pnl = (exit_price - entry_price) * shares
                roi = (pnl / bet_size) * 100 if bet_size > 0 else 0
                
                self.current_capital = Decimal(str(float(self.current_capital) + bet_size + pnl))
                
                if pnl > 0:
                    self.consecutive_wins += 1
                    self.consecutive_losses = 0
                else:
                    self.consecutive_losses += 1
                    self.consecutive_wins = 0
                
                if self.current_capital > self.peak_capital:
                    self.peak_capital = self.current_capital
                
                trade_record = {
                    **pos,
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'pnl': pnl,
                    'roi': roi,
                    'status': 'CLOSED'
                }
                self.trade_history.append(trade_record)
                self.open_positions.pop(i)
                
                self.db.update_trade(pos.get('db_id'), {
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'pnl': pnl,
                    'roi': roi,
                    'status': 'CLOSED'
                })
                
                logger.info(f"💰 Position closed: P&L ${pnl:+.2f} ({roi:+.1f}%) | Capital: ${self.current_capital:.2f}")
                return
    
    def get_consecutive_streak(self) -> Dict:
        return {
            'wins': self.consecutive_wins,
            'losses': self.consecutive_losses
        }
    
    def get_stats(self) -> Dict:
        total_trades = len(self.trade_history)
        if total_trades == 0:
            return {
                'total_trades': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'roi': 0,
                'current_capital': float(self.current_capital)
            }
        
        wins = len([t for t in self.trade_history if t.get('pnl', 0) > 0])
        total_pnl = sum(t.get('pnl', 0) for t in self.trade_history)
        total_roi = ((float(self.current_capital) - float(self.initial_capital)) / float(self.initial_capital)) * 100
        drawdown = ((float(self.peak_capital) - float(self.current_capital)) / float(self.peak_capital)) * 100
        
        return {
            'total_trades': total_trades,
            'win_rate': wins / total_trades if total_trades > 0 else 0,
            'total_pnl': total_pnl,
            'roi': total_roi,
            'current_capital': float(self.current_capital),
            'open_positions': len(self.open_positions),
            'max_drawdown': drawdown
        }