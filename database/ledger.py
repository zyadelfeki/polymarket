#!/usr/bin/env python3
"""
Production-Grade Ledger Manager

Enforces double-entry accounting for all capital movements.
Provides single source of truth for equity, PnL, positions.

Every transaction MUST balance (sum of lines = 0).
No shortcuts, no fake PnL, no estimation.
"""

import sqlite3
import json
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from contextlib import contextmanager
import logging

logger = logging.getLogger(__name__)

class LedgerError(Exception):
    """Raised when ledger operations violate double-entry rules"""
    pass

class Ledger:
    """
    Double-entry ledger for trading bot.
    
    Core principles:
    1. Every transaction has >= 2 lines that sum to zero
    2. Equity = Assets - Liabilities
    3. All PnL calculations derive from ledger, not synthetic prices
    4. Positions track quantity + entry price, not floating value
    """
    
    def __init__(self, db_path: str = "data/trading.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema"""
        with open("database/schema.sql", "r") as f:
            schema = f.read()
        
        with self._get_connection() as conn:
            conn.executescript(schema)
            conn.commit()
        
        logger.info(f"Ledger initialized at {self.db_path}")
    
    @contextmanager
    def _get_connection(self):
        """Context manager for DB connections"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    def get_equity(self) -> Decimal:
        """
        Get current equity from ledger.
        
        Equity = SUM(Assets) - SUM(Liabilities) + Unrealized PnL
        
        Returns:
            Current equity in USDC
        """
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    SUM(CASE WHEN a.account_type = 'ASSET' THEN tl.amount ELSE 0 END) as total_assets,
                    SUM(CASE WHEN a.account_type = 'LIABILITY' THEN tl.amount ELSE 0 END) as total_liabilities
                FROM transaction_lines tl
                JOIN accounts a ON tl.account_id = a.id
            """)
            row = cursor.fetchone()
            
            assets = Decimal(str(row['total_assets'] or 0))
            liabilities = Decimal(str(row['total_liabilities'] or 0))
            
            # Get unrealized PnL from open positions
            unrealized = self.get_unrealized_pnl()
            
            equity = assets - liabilities + unrealized
            
            logger.debug(f"Equity calculation: Assets={assets}, Liab={liabilities}, Unrealized={unrealized}, Total={equity}")
            
            return equity
    
    def get_unrealized_pnl(self) -> Decimal:
        """
        Calculate unrealized PnL from open positions.
        
        Formula: SUM((current_price - entry_price) * quantity) for all open positions
        """
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    SUM((current_price - entry_price) * quantity) as unrealized
                FROM positions
                WHERE status = 'OPEN' AND current_price IS NOT NULL
            """)
            row = cursor.fetchone()
            unrealized = row['unrealized'] or 0
            return Decimal(str(unrealized))
    
    def record_deposit(self, amount: Decimal, description: str = "Initial deposit") -> int:
        """
        Record capital deposit.
        
        Double-entry:
        - DR cash (asset)
        - CR equity (retained earnings)
        """
        if amount <= 0:
            raise LedgerError(f"Deposit amount must be positive: {amount}")
        
        with self._get_connection() as conn:
            # Create transaction
            cursor = conn.execute("""
                INSERT INTO transactions (transaction_type, description)
                VALUES ('DEPOSIT', ?)
            """, (description,))
            txn_id = cursor.lastrowid
            
            # Get account IDs
            cash_id = self._get_account_id(conn, 'cash')
            equity_id = self._get_account_id(conn, 'retained_earnings')
            
            # DR cash
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, cash_id, float(amount)))
            
            # CR equity (negative = credit)
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, equity_id, float(-amount)))
            
            conn.commit()
            
            logger.info(f"Recorded deposit: ${amount} (txn_id={txn_id})")
            return txn_id
    
    def record_trade_entry(
        self,
        strategy: str,
        market_id: str,
        token_id: str,
        side: str,
        quantity: Decimal,
        entry_price: Decimal,
        fees: Decimal,
        order_id: str,
        metadata: Optional[Dict] = None
    ) -> Tuple[int, int]:
        """
        Record trade entry (opening position).
        
        Double-entry:
        - DR positions_open (asset) = quantity * entry_price
        - DR trading_fees (expense) = fees
        - CR cash (asset) = -(quantity * entry_price + fees)
        
        Returns:
            (transaction_id, position_id)
        """
        if quantity <= 0:
            raise LedgerError(f"Quantity must be positive: {quantity}")
        if entry_price <= 0 or entry_price > 1:
            raise LedgerError(f"Invalid entry price: {entry_price}")
        
        cost = quantity * entry_price
        total_cost = cost + fees
        
        with self._get_connection() as conn:
            # Create transaction
            cursor = conn.execute("""
                INSERT INTO transactions (transaction_type, strategy, reference_id, description, metadata)
                VALUES ('TRADE_ENTRY', ?, ?, ?, ?)
            """, (
                strategy,
                order_id,
                f"Enter {side} position on {market_id[:20]}",
                json.dumps(metadata) if metadata else None
            ))
            txn_id = cursor.lastrowid
            
            # Get account IDs
            cash_id = self._get_account_id(conn, 'cash')
            positions_id = self._get_account_id(conn, 'positions_open')
            fees_id = self._get_account_id(conn, 'trading_fees')
            
            # DR positions_open
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, positions_id, float(cost)))
            
            # DR trading_fees
            if fees > 0:
                conn.execute("""
                    INSERT INTO transaction_lines (transaction_id, account_id, amount)
                    VALUES (?, ?, ?)
                """, (txn_id, fees_id, float(fees)))
            
            # CR cash
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, cash_id, float(-total_cost)))
            
            # Create position record
            cursor = conn.execute("""
                INSERT INTO positions (
                    market_id, token_id, side, quantity, entry_price,
                    entry_transaction_id, strategy, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                market_id, token_id, side, float(quantity), float(entry_price),
                txn_id, strategy, json.dumps(metadata) if metadata else None
            ))
            position_id = cursor.lastrowid
            
            conn.commit()
            
            logger.info(
                f"Recorded trade entry: {strategy} | {side} {quantity} @ {entry_price} | "
                f"Cost: ${total_cost} | Position ID: {position_id}"
            )
            
            return txn_id, position_id
    
    def record_trade_exit(
        self,
        position_id: int,
        exit_price: Decimal,
        fees: Decimal,
        exit_reason: str,
        order_id: str
    ) -> int:
        """
        Record trade exit (closing position).
        
        Double-entry:
        - DR cash (asset) = quantity * exit_price - fees
        - DR trading_fees (expense) = fees
        - CR positions_open (asset) = -(quantity * entry_price)
        - DR/CR trading_profit (revenue) = realized PnL
        
        Returns:
            transaction_id
        """
        with self._get_connection() as conn:
            # Get position details
            cursor = conn.execute("""
                SELECT * FROM positions WHERE id = ? AND status = 'OPEN'
            """, (position_id,))
            pos = cursor.fetchone()
            
            if not pos:
                raise LedgerError(f"Position {position_id} not found or already closed")
            
            quantity = Decimal(str(pos['quantity']))
            entry_price = Decimal(str(pos['entry_price']))
            strategy = pos['strategy']
            market_id = pos['market_id']
            
            proceeds = quantity * exit_price - fees
            cost = quantity * entry_price
            realized_pnl = proceeds - cost
            
            # Create transaction
            cursor = conn.execute("""
                INSERT INTO transactions (transaction_type, strategy, reference_id, description)
                VALUES ('TRADE_EXIT', ?, ?, ?)
            """, (
                strategy,
                order_id,
                f"Exit position {position_id}: {exit_reason}"
            ))
            txn_id = cursor.lastrowid
            
            # Get account IDs
            cash_id = self._get_account_id(conn, 'cash')
            positions_id = self._get_account_id(conn, 'positions_open')
            fees_id = self._get_account_id(conn, 'trading_fees')
            profit_id = self._get_account_id(conn, 'trading_profit')
            
            # DR cash (proceeds after fees)
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, cash_id, float(proceeds)))
            
            # DR trading_fees
            if fees > 0:
                conn.execute("""
                    INSERT INTO transaction_lines (transaction_id, account_id, amount)
                    VALUES (?, ?, ?)
                """, (txn_id, fees_id, float(fees)))
            
            # CR positions_open (remove cost basis)
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, positions_id, float(-cost)))
            
            # Record realized PnL
            # If profit: DR trading_profit (negative = credit, increases revenue)
            # If loss: CR trading_profit (positive = debit, decreases revenue)
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, profit_id, float(-realized_pnl)))
            
            # Close position
            conn.execute("""
                UPDATE positions
                SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP, current_price = ?
                WHERE id = ?
            """, (float(exit_price), position_id))
            
            conn.commit()
            
            logger.info(
                f"Recorded trade exit: Position {position_id} | Exit @ {exit_price} | "
                f"Realized PnL: ${realized_pnl:+.2f} | Reason: {exit_reason}"
            )
            
            return txn_id
    
    def update_position_prices(self, price_updates: Dict[str, Decimal]):
        """
        Update current prices for open positions.
        
        Args:
            price_updates: {token_id: current_price}
        """
        with self._get_connection() as conn:
            for token_id, price in price_updates.items():
                conn.execute("""
                    UPDATE positions
                    SET current_price = ?
                    WHERE token_id = ? AND status = 'OPEN'
                """, (float(price), token_id))
            conn.commit()
    
    def get_open_positions(self, strategy: Optional[str] = None) -> List[Dict]:
        """
        Get all open positions.
        
        Returns:
            List of position dicts with unrealized PnL
        """
        with self._get_connection() as conn:
            query = """
                SELECT 
                    p.*,
                    (p.current_price - p.entry_price) * p.quantity as unrealized_pnl,
                    ((p.current_price - p.entry_price) / p.entry_price) as unrealized_roi,
                    (julianday('now') - julianday(p.opened_at)) * 86400 as hold_time_seconds
                FROM positions p
                WHERE p.status = 'OPEN'
            """
            
            if strategy:
                query += " AND p.strategy = ?"
                cursor = conn.execute(query, (strategy,))
            else:
                cursor = conn.execute(query)
            
            positions = [dict(row) for row in cursor.fetchall()]
            return positions
    
    def get_strategy_pnl(self, strategy: str, days: int = 30) -> Dict:
        """
        Get PnL summary for a strategy.
        
        Returns:
            Dict with total_pnl, trade_count, win_rate, etc.
        """
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    t.strategy,
                    COUNT(DISTINCT t.id) as trade_count,
                    SUM(CASE WHEN tl.amount < 0 THEN -tl.amount ELSE 0 END) as total_profit,
                    SUM(CASE WHEN tl.amount > 0 THEN tl.amount ELSE 0 END) as total_loss,
                    SUM(-tl.amount) as net_pnl
                FROM transactions t
                JOIN transaction_lines tl ON t.id = tl.transaction_id
                JOIN accounts a ON tl.account_id = a.id
                WHERE a.name = 'trading_profit'
                  AND t.strategy = ?
                  AND t.timestamp >= datetime('now', ? || ' days')
                GROUP BY t.strategy
            """, (strategy, -days))
            
            row = cursor.fetchone()
            if not row:
                return {
                    'strategy': strategy,
                    'trade_count': 0,
                    'net_pnl': Decimal('0'),
                    'win_rate': 0.0
                }
            
            return dict(row)
    
    def validate_ledger(self) -> bool:
        """
        Validate that all transactions balance (double-entry invariant).
        
        Returns:
            True if valid, raises LedgerError otherwise
        """
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT transaction_id, SUM(amount) as balance
                FROM transaction_lines
                GROUP BY transaction_id
                HAVING ABS(balance) > 0.01
            """)
            
            unbalanced = cursor.fetchall()
            if unbalanced:
                errors = [f"Transaction {row['transaction_id']}: balance={row['balance']}" 
                         for row in unbalanced]
                raise LedgerError(f"Unbalanced transactions found: {errors}")
            
            logger.info("Ledger validation passed: all transactions balanced")
            return True
    
    def _get_account_id(self, conn, account_name: str) -> int:
        """Get account ID by name"""
        cursor = conn.execute("SELECT id FROM accounts WHERE name = ?", (account_name,))
        row = cursor.fetchone()
        if not row:
            raise LedgerError(f"Account not found: {account_name}")
        return row['id']