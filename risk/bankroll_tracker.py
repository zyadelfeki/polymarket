"""
Bankroll Tracker
Real-time capital and performance tracking
"""
from typing import Dict, List
from decimal import Decimal
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class BankrollTracker:
    """Performance and capital tracking"""
    
    def __init__(self, initial_capital: Decimal):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.peak_capital = initial_capital
        
        self.trade_history: List[Dict] = []
        self.daily_snapshots: List[Dict] = []
        
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_profit = Decimal("0")
    
    def record_trade(self, profit: Decimal, trade_details: Dict):
        """Record completed trade"""
        self.current_capital += profit
        self.total_profit += profit
        self.total_trades += 1
        
        if profit > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital
        
        trade_record = {
            "timestamp": datetime.utcnow(),
            "profit": profit,
            "capital_after": self.current_capital,
            "details": trade_details
        }
        self.trade_history.append(trade_record)
        
        logger.info(f"📊 Capital: ${self.current_capital:.2f} ({self.get_total_return():.1f}%)")
    
    def get_available_capital(self) -> Decimal:
        """Get capital available for trading (excluding reserved)"""
        # Keep 50% in reserve
        return self.current_capital * Decimal("0.5")
    
    def get_total_return(self) -> float:
        """Total return percentage"""
        if self.initial_capital > 0:
            return float((self.current_capital - self.initial_capital) / self.initial_capital * 100)
        return 0.0
    
    def get_win_rate(self) -> float:
        """Calculate win rate"""
        if self.total_trades > 0:
            return (self.winning_trades / self.total_trades) * 100
        return 0.0
    
    def get_sharpe_ratio(self) -> float:
        """Calculate Sharpe ratio (simplified)"""
        if len(self.trade_history) < 2:
            return 0.0
        
        returns = [float(t["profit"]) for t in self.trade_history]
        avg_return = sum(returns) / len(returns)
        
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
        std_dev = variance ** 0.5
        
        if std_dev > 0:
            return avg_return / std_dev
        return 0.0
    
    def get_stats(self) -> Dict:
        """Comprehensive statistics"""
        return {
            "current_capital": float(self.current_capital),
            "initial_capital": float(self.initial_capital),
            "peak_capital": float(self.peak_capital),
            "total_return_pct": self.get_total_return(),
            "total_profit": float(self.total_profit),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": self.get_win_rate(),
            "sharpe_ratio": self.get_sharpe_ratio(),
            "available_capital": float(self.get_available_capital())
        }
    
    def print_summary(self):
        """Print formatted summary"""
        stats = self.get_stats()
        
        print("\n" + "="*60)
        print("📊 PERFORMANCE SUMMARY")
        print("="*60)
        print(f"💵 Capital: ${stats['current_capital']:.2f} (from ${stats['initial_capital']:.2f})")
        print(f"📈 Total Return: {stats['total_return_pct']:+.1f}%")
        print(f"🎯 Win Rate: {stats['win_rate_pct']:.1f}% ({stats['winning_trades']}/{stats['total_trades']})")
        print(f"📊 Sharpe Ratio: {stats['sharpe_ratio']:.2f}")
        print(f"🔺 Peak: ${stats['peak_capital']:.2f}")
        print("="*60 + "\n")