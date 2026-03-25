"""Performance metrics — moved from backtest/ (now consolidated into backtesting/)."""
import numpy as np
from typing import List, Dict
import logging

logger = logging.getLogger(__name__)

class PerformanceMetrics:
    @staticmethod
    def calculate_sharpe_ratio(returns: List[float], risk_free_rate: float = 0.0) -> float:
        if not returns or len(returns) < 2:
            return 0.0
        returns_array = np.array(returns)
        excess_returns = returns_array - risk_free_rate
        if np.std(excess_returns) == 0:
            return 0.0
        return np.mean(excess_returns) / np.std(excess_returns)
    
    @staticmethod
    def calculate_max_drawdown(equity_curve: List[float]) -> float:
        if not equity_curve or len(equity_curve) < 2:
            return 0.0
        peak = equity_curve[0]
        max_dd = 0.0
        for value in equity_curve:
            if value > peak:
                peak = value
            dd = (peak - value) / peak * 100
            if dd > max_dd:
                max_dd = dd
        return max_dd
    
    @staticmethod
    def calculate_win_rate(trades: List[Dict]) -> float:
        if not trades:
            return 0.0
        wins = len([t for t in trades if t.get('pnl', 0) > 0])
        return wins / len(trades)
    
    @staticmethod
    def calculate_profit_factor(trades: List[Dict]) -> float:
        if not trades:
            return 0.0
        gross_profit = sum(t.get('pnl', 0) for t in trades if t.get('pnl', 0) > 0)
        gross_loss = abs(sum(t.get('pnl', 0) for t in trades if t.get('pnl', 0) < 0))
        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0.0
        return gross_profit / gross_loss
    
    @staticmethod
    def calculate_avg_trade_pnl(trades: List[Dict]) -> float:
        if not trades:
            return 0.0
        return sum(t.get('pnl', 0) for t in trades) / len(trades)
    
    @staticmethod
    def generate_report(trades: List[Dict], initial_capital: float, final_capital: float) -> Dict:
        if not trades:
            return {'error': 'No trades to analyze'}
        
        returns = [t.get('roi', 0) / 100 for t in trades if t.get('roi') is not None]
        equity_curve = [initial_capital]
        running_capital = initial_capital
        for trade in trades:
            running_capital += trade.get('pnl', 0)
            equity_curve.append(running_capital)
        
        return {
            'total_trades': len(trades),
            'win_rate': PerformanceMetrics.calculate_win_rate(trades),
            'profit_factor': PerformanceMetrics.calculate_profit_factor(trades),
            'sharpe_ratio': PerformanceMetrics.calculate_sharpe_ratio(returns),
            'max_drawdown': PerformanceMetrics.calculate_max_drawdown(equity_curve),
            'avg_trade_pnl': PerformanceMetrics.calculate_avg_trade_pnl(trades),
            'total_pnl': final_capital - initial_capital,
            'roi': ((final_capital - initial_capital) / initial_capital) * 100
        }
