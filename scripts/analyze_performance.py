#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.db import Database
from backtest.performance_metrics import PerformanceMetrics
from config.settings import settings

def analyze():
    print("\n" + "="*60)
    print("📊 PERFORMANCE ANALYSIS")
    print("="*60 + "\n")
    
    db = Database()
    stats = db.get_performance_stats(days=30)
    
    if not stats:
        print("⚠️  No trade history found\n")
        return
    
    print(f"Total Trades: {stats['total_trades']}")
    print(f"Wins: {stats['wins']} | Losses: {stats['losses']}")
    print(f"Win Rate: {stats['win_rate']:.1%}")
    print(f"Total P&L: ${stats['total_pnl']:+.2f}")
    print(f"Avg P&L per Trade: ${stats['avg_pnl_per_trade']:+.2f}")
    
    session = db.get_session()
    try:
        from utils.db import Trade
        trades = session.query(Trade).filter(Trade.status == 'CLOSED').all()
        
        if trades:
            trade_dicts = [{
                'pnl': t.pnl,
                'roi': t.roi
            } for t in trades]
            
            initial = float(settings.INITIAL_CAPITAL)
            final = initial + stats['total_pnl']
            
            report = PerformanceMetrics.generate_report(trade_dicts, initial, final)
            
            print(f"\nSharpe Ratio: {report['sharpe_ratio']:.2f}")
            print(f"Max Drawdown: {report['max_drawdown']:.1f}%")
            print(f"Profit Factor: {report['profit_factor']:.2f}")
            print(f"ROI: {report['roi']:+.1f}%")
    finally:
        session.close()
    
    print("\n" + "="*60 + "\n")

if __name__ == "__main__":
    analyze()