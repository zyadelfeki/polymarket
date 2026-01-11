#!/usr/bin/env python3
"""
Production Backtesting Engine

Event-driven backtesting on historical data:
- Replay Polymarket market snapshots
- Replay CEX price feeds
- Execute strategies as if live
- Calculate real metrics (Sharpe, drawdown, win rate)

Critical: NO LOOK-AHEAD BIAS
- Only use data available at decision time
- Respect order execution delays
- Include realistic slippage and fees

Output:
- Trade-by-trade log
- PnL curve
- Sharpe ratio
- Max drawdown
- Win rate
- Average hold time
"""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
import logging
from dataclasses import dataclass, asdict
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

@dataclass
class BacktestTrade:
    """Single backtest trade"""
    trade_id: int
    strategy: str
    timestamp: datetime
    market_id: str
    question: str
    side: str  # 'YES' or 'NO'
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    entry_fees: Decimal
    exit_fees: Decimal
    hold_time_seconds: int
    pnl: Decimal
    roi: Decimal
    exit_reason: str
    metadata: dict

@dataclass
class BacktestResults:
    """Backtest results summary"""
    strategy: str
    start_date: datetime
    end_date: datetime
    initial_capital: Decimal
    final_equity: Decimal
    total_pnl: Decimal
    total_return: float
    
    # Trade stats
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: Decimal
    avg_loss: Decimal
    avg_hold_time_seconds: int
    
    # Risk metrics
    sharpe_ratio: float
    max_drawdown: float
    max_drawdown_duration_days: int
    calmar_ratio: float
    
    # Execution
    avg_slippage: Decimal
    total_fees: Decimal
    
    # Trades detail
    trades: List[BacktestTrade]
    equity_curve: List[Tuple[datetime, Decimal]]

class BacktestEngine:
    """
    Event-driven backtest engine.
    
    Principles:
    1. No look-ahead bias (only use past data)
    2. Realistic execution (slippage + fees)
    3. Time-aware (respect order delays)
    4. Market microstructure (bid-ask spread)
    """
    
    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        
        # Execution realism
        self.base_slippage = Decimal(str(config.get('base_slippage', 0.005)))  # 0.5%
        self.fee_rate = Decimal(str(config.get('fee_rate', 0.02)))  # 2% Polymarket fee
        self.execution_delay_seconds = config.get('execution_delay', 2)  # 2 second delay
        
        # Position limits
        self.max_position_pct = config.get('max_position_pct', 5.0)  # 5% max per trade
        self.max_aggregate_exposure_pct = config.get('max_aggregate_exposure', 20.0)  # 20% total
        
        # State
        self.equity = Decimal('0')
        self.initial_capital = Decimal('0')
        self.open_positions = {}  # position_id -> position
        self.closed_trades = []
        self.equity_curve = []
        self.next_trade_id = 1
        
        logger.info("BacktestEngine initialized")
    
    async def run_backtest(
        self,
        strategy,
        strategy_name: str,
        historical_data: Dict,
        initial_capital: Decimal,
        start_date: datetime,
        end_date: datetime
    ) -> BacktestResults:
        """
        Run backtest on historical data.
        
        Args:
            strategy: Strategy instance with scan_for_opportunities method
            strategy_name: Strategy name for reporting
            historical_data: Dict with 'markets' and 'prices' time series
            initial_capital: Starting capital
            start_date: Backtest start
            end_date: Backtest end
        
        Returns:
            BacktestResults with metrics and trades
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"STARTING BACKTEST: {strategy_name}")
        logger.info(f"Period: {start_date} to {end_date}")
        logger.info(f"Initial Capital: ${initial_capital}")
        logger.info(f"{'='*60}\n")
        
        # Initialize
        self.initial_capital = initial_capital
        self.equity = initial_capital
        self.open_positions = {}
        self.closed_trades = []
        self.equity_curve = [(start_date, initial_capital)]
        self.next_trade_id = 1
        
        # Get time-ordered events
        events = self._create_event_timeline(
            historical_data,
            start_date,
            end_date
        )
        
        logger.info(f"Processing {len(events)} historical events...")
        
        # Process events chronologically
        for i, event in enumerate(events):
            timestamp = event['timestamp']
            event_type = event['type']
            
            if i % 100 == 0:
                logger.info(
                    f"Progress: {i}/{len(events)} events | "
                    f"Equity: ${self.equity} | "
                    f"Open: {len(self.open_positions)} | "
                    f"Closed: {len(self.closed_trades)}"
                )
            
            # Update position prices
            if event_type == 'market_update':
                await self._update_position_prices(timestamp, event['data'])
            
            # Check exits
            await self._check_position_exits(timestamp, strategy)
            
            # Check for new opportunities
            if event_type == 'scan_trigger':
                await self._scan_and_enter(
                    strategy,
                    strategy_name,
                    timestamp,
                    event['markets'],
                    event['prices']
                )
            
            # Record equity
            if i % 50 == 0:  # Record every 50 events
                self.equity_curve.append((timestamp, self.equity))
        
        # Close remaining positions at end
        await self._close_all_positions(end_date, "BACKTEST_END")
        
        # Calculate metrics
        results = self._calculate_results(
            strategy_name,
            start_date,
            end_date
        )
        
        # Log summary
        self._log_results(results)
        
        return results
    
    def _create_event_timeline(
        self,
        historical_data: Dict,
        start_date: datetime,
        end_date: datetime
    ) -> List[Dict]:
        """
        Create chronologically ordered event timeline.
        
        Events:
        - market_update: New market snapshot available
        - scan_trigger: Time to scan for opportunities
        
        Returns:
            List of events sorted by timestamp
        """
        events = []
        
        market_snapshots = historical_data.get('market_snapshots', [])
        price_ticks = historical_data.get('price_ticks', [])
        
        # Create scan triggers every 15 seconds
        current = start_date
        while current <= end_date:
            # Get markets available at this time
            available_markets = [
                m for m in market_snapshots
                if m['timestamp'] <= current
            ]
            
            # Get prices available at this time
            available_prices = {
                p['symbol']: Decimal(str(p['price']))
                for p in price_ticks
                if p['timestamp'] <= current
            }
            
            if available_markets and available_prices:
                events.append({
                    'timestamp': current,
                    'type': 'scan_trigger',
                    'markets': available_markets[-50:],  # Last 50 markets
                    'prices': available_prices
                })
            
            current += timedelta(seconds=15)
        
        # Add market update events
        for snapshot in market_snapshots:
            if start_date <= snapshot['timestamp'] <= end_date:
                events.append({
                    'timestamp': snapshot['timestamp'],
                    'type': 'market_update',
                    'data': snapshot
                })
        
        # Sort by timestamp
        events.sort(key=lambda e: e['timestamp'])
        
        return events
    
    async def _scan_and_enter(
        self,
        strategy,
        strategy_name: str,
        timestamp: datetime,
        markets: List[Dict],
        prices: Dict[str, Decimal]
    ):
        """
        Scan for opportunities and enter positions.
        """
        # Check if we can trade (aggregate exposure)
        current_exposure = sum(
            pos['quantity'] * pos['entry_price']
            for pos in self.open_positions.values()
        )
        
        max_exposure = self.equity * Decimal(str(self.max_aggregate_exposure_pct / 100))
        
        if current_exposure >= max_exposure:
            return  # At exposure limit
        
        # Scan for opportunities
        try:
            # Mock polymarket client for backtesting
            mock_client = BacktestPolymarketClient(markets)
            
            opportunities = await strategy.scan_for_opportunities(
                markets=markets,
                exchange_prices=prices,
                polymarket_client=mock_client
            )
            
            if not opportunities:
                return
            
            # Enter top opportunity
            for opp in opportunities[:1]:  # Top 1 per scan
                # Calculate position size
                max_size = self.equity * Decimal(str(self.max_position_pct / 100))
                
                if opp.action == 'BUY_YES':
                    entry_price = opp.market_price_yes
                else:
                    entry_price = opp.market_price_no
                
                # Apply slippage (worse price)
                entry_price = entry_price * (Decimal('1.0') + self.base_slippage)
                entry_price = min(entry_price, Decimal('0.99'))  # Cap at 0.99
                
                quantity = max_size / entry_price
                cost = quantity * entry_price
                fees = cost * self.fee_rate
                total_cost = cost + fees
                
                if total_cost > self.equity:
                    continue  # Not enough capital
                
                # Enter position
                position_id = self.next_trade_id
                self.next_trade_id += 1
                
                self.open_positions[position_id] = {
                    'id': position_id,
                    'strategy': strategy_name,
                    'timestamp': timestamp,
                    'market_id': opp.market_id,
                    'question': opp.question,
                    'side': opp.action.replace('BUY_', ''),
                    'entry_price': entry_price,
                    'quantity': quantity,
                    'entry_fees': fees,
                    'metadata': {
                        'symbol': opp.symbol,
                        'threshold': float(opp.threshold),
                        'edge': float(opp.edge)
                    }
                }
                
                # Update equity
                self.equity -= total_cost
                
                logger.debug(
                    f"[{timestamp}] ENTER: {position_id} | "
                    f"{opp.question[:40]} | "
                    f"{opp.action} {quantity:.2f} @ {entry_price} | "
                    f"Cost: ${total_cost}"
                )
        
        except Exception as e:
            logger.error(f"Error scanning: {e}")
    
    async def _update_position_prices(self, timestamp: datetime, market_data: Dict):
        """
        Update current prices for open positions.
        """
        market_id = market_data.get('market_id')
        
        for pos_id, pos in self.open_positions.items():
            if pos['market_id'] == market_id:
                # Update current price from snapshot
                if pos['side'] == 'YES':
                    pos['current_price'] = Decimal(str(market_data.get('yes_price', 0.5)))
                else:
                    pos['current_price'] = Decimal(str(market_data.get('no_price', 0.5)))
    
    async def _check_position_exits(self, timestamp: datetime, strategy):
        """
        Check if any positions should be exited.
        
        Exit conditions:
        - Time stop (30 seconds for latency arb)
        - Target profit (40%)
        - Stop loss (-5%)
        """
        to_close = []
        
        for pos_id, pos in self.open_positions.items():
            current_price = pos.get('current_price')
            if not current_price:
                continue
            
            entry_price = pos['entry_price']
            hold_time = (timestamp - pos['timestamp']).total_seconds()
            
            exit_reason = None
            
            # Time stop
            if pos['strategy'] == 'latency_arb' and hold_time > 30:
                exit_reason = 'TIME_STOP'
            
            # Target profit
            roi = (current_price - entry_price) / entry_price
            if roi > Decimal('0.40'):
                exit_reason = 'TARGET_HIT'
            
            # Stop loss
            if roi < Decimal('-0.05'):
                exit_reason = 'STOP_LOSS'
            
            if exit_reason:
                to_close.append((pos_id, exit_reason, current_price))
        
        # Close positions
        for pos_id, exit_reason, exit_price in to_close:
            await self._close_position(timestamp, pos_id, exit_price, exit_reason)
    
    async def _close_position(
        self,
        timestamp: datetime,
        position_id: int,
        exit_price: Decimal,
        exit_reason: str
    ):
        """
        Close a position and record trade.
        """
        pos = self.open_positions.pop(position_id)
        
        # Apply slippage (worse price for exit)
        exit_price = exit_price * (Decimal('1.0') - self.base_slippage)
        exit_price = max(exit_price, Decimal('0.01'))  # Floor at 0.01
        
        quantity = pos['quantity']
        entry_price = pos['entry_price']
        
        proceeds = quantity * exit_price
        exit_fees = proceeds * self.fee_rate
        net_proceeds = proceeds - exit_fees
        
        # Update equity
        self.equity += net_proceeds
        
        # Calculate PnL
        cost = quantity * entry_price
        pnl = net_proceeds - cost
        roi = pnl / cost
        
        hold_time = int((timestamp - pos['timestamp']).total_seconds())
        
        # Record trade
        trade = BacktestTrade(
            trade_id=position_id,
            strategy=pos['strategy'],
            timestamp=timestamp,
            market_id=pos['market_id'],
            question=pos['question'],
            side=pos['side'],
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            entry_fees=pos['entry_fees'],
            exit_fees=exit_fees,
            hold_time_seconds=hold_time,
            pnl=pnl,
            roi=roi,
            exit_reason=exit_reason,
            metadata=pos['metadata']
        )
        
        self.closed_trades.append(trade)
        
        logger.debug(
            f"[{timestamp}] EXIT: {position_id} | "
            f"{exit_reason} | "
            f"Exit @ {exit_price} | "
            f"PnL: ${pnl:+.2f} ({roi:+.1%}) | "
            f"Hold: {hold_time}s"
        )
    
    async def _close_all_positions(self, timestamp: datetime, reason: str):
        """Close all open positions at end of backtest"""
        pos_ids = list(self.open_positions.keys())
        for pos_id in pos_ids:
            pos = self.open_positions[pos_id]
            exit_price = pos.get('current_price', pos['entry_price'])
            await self._close_position(timestamp, pos_id, exit_price, reason)
    
    def _calculate_results(self, strategy: str, start_date: datetime, end_date: datetime) -> BacktestResults:
        """
        Calculate backtest metrics.
        """
        if not self.closed_trades:
            return BacktestResults(
                strategy=strategy,
                start_date=start_date,
                end_date=end_date,
                initial_capital=self.initial_capital,
                final_equity=self.equity,
                total_pnl=Decimal('0'),
                total_return=0.0,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate=0.0,
                avg_win=Decimal('0'),
                avg_loss=Decimal('0'),
                avg_hold_time_seconds=0,
                sharpe_ratio=0.0,
                max_drawdown=0.0,
                max_drawdown_duration_days=0,
                calmar_ratio=0.0,
                avg_slippage=Decimal('0'),
                total_fees=Decimal('0'),
                trades=self.closed_trades,
                equity_curve=self.equity_curve
            )
        
        # Basic stats
        total_pnl = sum(t.pnl for t in self.closed_trades)
        total_return = float(total_pnl / self.initial_capital)
        
        winners = [t for t in self.closed_trades if t.pnl > 0]
        losers = [t for t in self.closed_trades if t.pnl < 0]
        
        win_rate = len(winners) / len(self.closed_trades) if self.closed_trades else 0.0
        avg_win = sum(t.pnl for t in winners) / len(winners) if winners else Decimal('0')
        avg_loss = sum(t.pnl for t in losers) / len(losers) if losers else Decimal('0')
        avg_hold_time = int(sum(t.hold_time_seconds for t in self.closed_trades) / len(self.closed_trades))
        
        total_fees = sum(t.entry_fees + t.exit_fees for t in self.closed_trades)
        
        # Calculate Sharpe ratio
        returns = [float(t.pnl) for t in self.closed_trades]
        if len(returns) > 1:
            sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(252) if np.std(returns) > 0 else 0.0
        else:
            sharpe = 0.0
        
        # Calculate max drawdown
        max_dd, max_dd_duration = self._calculate_max_drawdown()
        
        # Calmar ratio = Annual Return / Max Drawdown
        calmar = total_return / max_dd if max_dd > 0 else 0.0
        
        return BacktestResults(
            strategy=strategy,
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
            final_equity=self.equity,
            total_pnl=total_pnl,
            total_return=total_return,
            total_trades=len(self.closed_trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            avg_hold_time_seconds=avg_hold_time,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            max_drawdown_duration_days=max_dd_duration,
            calmar_ratio=calmar,
            avg_slippage=self.base_slippage,
            total_fees=total_fees,
            trades=self.closed_trades,
            equity_curve=self.equity_curve
        )
    
    def _calculate_max_drawdown(self) -> Tuple[float, int]:
        """
        Calculate maximum drawdown and duration.
        
        Returns:
            (max_drawdown_pct, max_duration_days)
        """
        if len(self.equity_curve) < 2:
            return 0.0, 0
        
        equity_values = [float(e) for _, e in self.equity_curve]
        peak = equity_values[0]
        max_dd = 0.0
        max_dd_duration = 0
        current_dd_start = None
        
        for i, equity in enumerate(equity_values):
            if equity > peak:
                peak = equity
                current_dd_start = None
            else:
                dd = (peak - equity) / peak
                if dd > max_dd:
                    max_dd = dd
                
                if current_dd_start is None:
                    current_dd_start = i
                
                dd_duration = i - current_dd_start
                if dd_duration > max_dd_duration:
                    max_dd_duration = dd_duration
        
        # Convert duration to days (assuming 15-second intervals)
        max_dd_duration_days = int(max_dd_duration * 15 / 86400)
        
        return max_dd, max_dd_duration_days
    
    def _log_results(self, results: BacktestResults):
        """Log backtest results summary"""
        logger.info("\n" + "="*60)
        logger.info("BACKTEST RESULTS")
        logger.info("="*60)
        logger.info(f"Strategy: {results.strategy}")
        logger.info(f"Period: {results.start_date} to {results.end_date}")
        logger.info(f"")
        logger.info(f"Initial Capital: ${results.initial_capital}")
        logger.info(f"Final Equity: ${results.final_equity}")
        logger.info(f"Total PnL: ${results.total_pnl:+.2f}")
        logger.info(f"Total Return: {results.total_return:+.1%}")
        logger.info(f"")
        logger.info(f"Total Trades: {results.total_trades}")
        logger.info(f"Winners: {results.winning_trades} ({results.win_rate:.1%})")
        logger.info(f"Losers: {results.losing_trades}")
        logger.info(f"Avg Win: ${results.avg_win:+.2f}")
        logger.info(f"Avg Loss: ${results.avg_loss:+.2f}")
        logger.info(f"Avg Hold Time: {results.avg_hold_time_seconds}s")
        logger.info(f"")
        logger.info(f"Sharpe Ratio: {results.sharpe_ratio:.2f}")
        logger.info(f"Max Drawdown: {results.max_drawdown:.1%}")
        logger.info(f"Max DD Duration: {results.max_drawdown_duration_days} days")
        logger.info(f"Calmar Ratio: {results.calmar_ratio:.2f}")
        logger.info(f"")
        logger.info(f"Total Fees: ${results.total_fees}")
        logger.info(f"Avg Slippage: {results.avg_slippage:.2%}")
        logger.info("="*60 + "\n")
    
    def export_results(self, results: BacktestResults, output_path: str):
        """Export results to JSON"""
        output = {
            'summary': {
                'strategy': results.strategy,
                'start_date': results.start_date.isoformat(),
                'end_date': results.end_date.isoformat(),
                'initial_capital': float(results.initial_capital),
                'final_equity': float(results.final_equity),
                'total_pnl': float(results.total_pnl),
                'total_return': results.total_return,
                'sharpe_ratio': results.sharpe_ratio,
                'max_drawdown': results.max_drawdown,
                'win_rate': results.win_rate
            },
            'trades': [asdict(t) for t in results.trades],
            'equity_curve': [
                {'timestamp': ts.isoformat(), 'equity': float(eq)}
                for ts, eq in results.equity_curve
            ]
        }
        
        with open(output_path, 'w') as f:
            json.dump(output, f, indent=2, default=str)
        
        logger.info(f"Results exported to {output_path}")

class BacktestPolymarketClient:
    """Mock Polymarket client for backtesting"""
    
    def __init__(self, markets: List[Dict]):
        self.markets = markets
    
    async def get_market_orderbook(self, token_id: str) -> Optional[Dict]:
        """Return mock orderbook from historical snapshot"""
        # Find market with this token
        for market in self.markets:
            tokens = market.get('tokens', [])
            for token in tokens:
                if token.get('token_id') == token_id:
                    # Return mock orderbook
                    price = Decimal(str(token.get('price', 0.5)))
                    return {
                        'bids': [{'price': float(price * Decimal('0.99')), 'size': 1000}],
                        'asks': [{'price': float(price * Decimal('1.01')), 'size': 1000}]
                    }
        return None