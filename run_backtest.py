#!/usr/bin/env python3
"""
Backtest Runner

Run backtests on strategies using historical or mock data.

Usage:
    # Using mock data (for testing)
    python run_backtest.py --mock --days 7
    
    # Using real historical data
    python run_backtest.py --start 2026-01-01 --end 2026-01-10
    
    # Specific strategy
    python run_backtest.py --strategy latency_arb --mock
"""

import asyncio
import argparse
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from backtesting.backtest_engine import BacktestEngine
from backtesting.data_collector import DataCollector
from strategy.latency_arbitrage_engine import LatencyArbitrageEngine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

async def run_latency_arb_backtest(
    engine: BacktestEngine,
    historical_data: dict,
    initial_capital: Decimal,
    start_date: datetime,
    end_date: datetime
):
    """
    Run latency arbitrage backtest.
    """
    strategy = LatencyArbitrageEngine(config={
        'min_edge': 0.05,
        'max_hold_seconds': 30,
        'target_profit': 0.40,
        'stop_loss': 0.05
    })
    
    results = await engine.run_backtest(
        strategy=strategy,
        strategy_name='latency_arb',
        historical_data=historical_data,
        initial_capital=initial_capital,
        start_date=start_date,
        end_date=end_date
    )
    
    return results

async def main():
    parser = argparse.ArgumentParser(description='Run strategy backtests')
    parser.add_argument(
        '--strategy',
        choices=['latency_arb', 'all'],
        default='latency_arb',
        help='Strategy to backtest'
    )
    parser.add_argument(
        '--mock',
        action='store_true',
        help='Use mock data instead of historical'
    )
    parser.add_argument(
        '--days',
        type=int,
        default=7,
        help='Days of mock data to generate (if --mock)'
    )
    parser.add_argument(
        '--start',
        type=str,
        help='Start date (YYYY-MM-DD) for historical data'
    )
    parser.add_argument(
        '--end',
        type=str,
        help='End date (YYYY-MM-DD) for historical data'
    )
    parser.add_argument(
        '--capital',
        type=float,
        default=10000.0,
        help='Initial capital'
    )
    parser.add_argument(
        '--output',
        type=str,
        help='Output path for results JSON'
    )
    
    args = parser.parse_args()
    
    # Setup dates
    if args.mock:
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=args.days)
        logger.info(f"Using mock data: {args.days} days")
    else:
        if not args.start or not args.end:
            logger.error("--start and --end required when not using --mock")
            return
        start_date = datetime.fromisoformat(args.start)
        end_date = datetime.fromisoformat(args.end)
        logger.info(f"Using historical data: {start_date} to {end_date}")
    
    # Get data
    collector = DataCollector()
    
    if args.mock:
        historical_data = collector.generate_mock_data(
            start_date=start_date,
            end_date=end_date,
            num_markets=10
        )
    else:
        historical_data = collector.get_historical_data(
            start_date=start_date,
            end_date=end_date
        )
        
        if not historical_data['market_snapshots']:
            logger.error(
                f"No historical data found for period {start_date} to {end_date}. "
                f"Run data collector first or use --mock."
            )
            return
    
    # Setup backtest engine
    engine = BacktestEngine(config={
        'base_slippage': 0.005,  # 0.5%
        'fee_rate': 0.02,        # 2%
        'execution_delay': 2,
        'max_position_pct': 5.0,
        'max_aggregate_exposure': 20.0
    })
    
    initial_capital = Decimal(str(args.capital))
    
    # Run backtests
    if args.strategy == 'latency_arb' or args.strategy == 'all':
        logger.info("\n" + "="*60)
        logger.info("RUNNING LATENCY ARBITRAGE BACKTEST")
        logger.info("="*60 + "\n")
        
        results = await run_latency_arb_backtest(
            engine=engine,
            historical_data=historical_data,
            initial_capital=initial_capital,
            start_date=start_date,
            end_date=end_date
        )
        
        # Export results
        if args.output:
            engine.export_results(results, args.output)
        else:
            default_output = f"backtest_results_{results.strategy}_{start_date.date()}.json"
            engine.export_results(results, default_output)
        
        # Evaluate results
        evaluate_results(results)

def evaluate_results(results):
    """
    Evaluate backtest results against production criteria.
    """
    logger.info("\n" + "="*60)
    logger.info("PRODUCTION READINESS EVALUATION")
    logger.info("="*60)
    
    # Define minimum thresholds
    min_win_rate = 0.55  # 55%
    min_sharpe = 1.0
    max_drawdown = 0.15  # 15%
    min_trades = 10
    
    checks = []
    
    # Check 1: Win rate
    win_rate_ok = results.win_rate >= min_win_rate
    checks.append(('Win Rate', results.win_rate, f">= {min_win_rate:.0%}", win_rate_ok))
    
    # Check 2: Sharpe ratio
    sharpe_ok = results.sharpe_ratio >= min_sharpe
    checks.append(('Sharpe Ratio', results.sharpe_ratio, f">= {min_sharpe}", sharpe_ok))
    
    # Check 3: Max drawdown
    dd_ok = results.max_drawdown <= max_drawdown
    checks.append(('Max Drawdown', results.max_drawdown, f"<= {max_drawdown:.0%}", dd_ok))
    
    # Check 4: Total return positive
    return_ok = results.total_return > 0
    checks.append(('Total Return', results.total_return, '> 0%', return_ok))
    
    # Check 5: Minimum trades
    trades_ok = results.total_trades >= min_trades
    checks.append(('Total Trades', results.total_trades, f">= {min_trades}", trades_ok))
    
    # Display results
    for name, value, criterion, passed in checks:
        status = "✅ PASS" if passed else "❌ FAIL"
        if isinstance(value, float) and value < 100:
            value_str = f"{value:.2%}" if value < 1 else f"{value:.2f}"
        else:
            value_str = str(value)
        logger.info(f"{status} | {name}: {value_str} (criterion: {criterion})")
    
    logger.info("="*60)
    
    all_passed = all(check[3] for check in checks)
    
    if all_passed:
        logger.info("✅ STRATEGY PASSED ALL CHECKS - APPROVED FOR PAPER TRADING")
    else:
        logger.warning("❌ STRATEGY FAILED CHECKS - NEEDS IMPROVEMENT")
        logger.warning("Do not deploy to paper trading until all checks pass.")
    
    logger.info("="*60 + "\n")
    
    return all_passed

if __name__ == '__main__':
    asyncio.run(main())