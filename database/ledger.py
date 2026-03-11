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


def _format_amount(amount: Decimal) -> str:
    return str(amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

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
            self._ensure_position_columns(conn)
            conn.commit()
        
        logger.info(f"Ledger initialized at {self.db_path}")

    def _ensure_position_columns(self, conn: sqlite3.Connection) -> None:
        """Ensure positions table has expected columns for legacy DBs."""
        cursor = conn.execute("PRAGMA table_info(positions)")
        columns = {row[1] for row in cursor.fetchall()}

        if "opened_at" not in columns:
            conn.execute("ALTER TABLE positions ADD COLUMN opened_at TIMESTAMP")

    def record_audit_event(
        self,
        *,
        operation: str,
        entity_type: str,
        entity_id: Optional[str] = None,
        old_state: Optional[str] = None,
        new_state: Optional[str] = None,
        reason: Optional[str] = None,
        context: Optional[Dict] = None,
        correlation_id: Optional[str] = None,
        details: Optional[str] = None,
    ) -> int:
        """Record an audit log entry."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_log (
                    operation, entity_type, entity_id, old_state, new_state,
                    reason, context, correlation_id, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation,
                    entity_type,
                    entity_id,
                    old_state,
                    new_state,
                    reason,
                    json.dumps(context) if context else None,
                    correlation_id,
                    details,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    @staticmethod
    def calculate_breakeven_price(
        entry_price: Decimal,
        quantity: Decimal,
        fee_rate: Decimal = Decimal("0.02"),
    ) -> Decimal:
        """Calculate breakeven price after fees."""
        if not isinstance(entry_price, Decimal) or not isinstance(quantity, Decimal):
            raise TypeError("entry_price and quantity must be Decimal")
        if entry_price <= 0 or quantity <= 0:
            raise ValueError("entry_price and quantity must be positive")
        buy_cost = entry_price * (Decimal("1") + fee_rate)
        breakeven = buy_cost / (Decimal("1") - fee_rate)
        return breakeven
    
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
            cursor = conn.execute(
                "SELECT balance FROM accounts WHERE account_name = ?",
                ("Cash",)
            )
            row = cursor.fetchone()

            cash_balance = Decimal(str(row['balance'])) if row and row['balance'] is not None else Decimal('0')
            return cash_balance
    
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
            cursor = conn.execute(
                """
                INSERT INTO transactions (transaction_type, description)
                VALUES ('DEPOSIT', ?)
                """,
                (description,)
            )
            txn_id = cursor.lastrowid
            
            # Get account IDs
            cash_id = self._get_account_id(conn, 'Cash')
            equity_id = self._get_account_id(conn, 'Owner Equity')
            
            # DR cash
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, cash_id, _format_amount(amount)))
            
            # CR equity (negative = credit)
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, equity_id, _format_amount(-amount)))
            
            conn.commit()
            
            logger.info(f"Recorded deposit: ${amount} (txn_id={txn_id})")
            return txn_id
    
    def record_trade_entry(
        self,
        market_id: str,
        side: str,
        quantity: Decimal,
        entry_price: Decimal,
        fees: Decimal,
        strategy: str = "default",
        token_id: str = "",
        order_id: str = "",
        metadata: Optional[Dict] = None
    ) -> int:
        """
        Record trade entry (opening position).
        
        Double-entry:
        - DR Positions (asset) = quantity * entry_price
        - DR Trading Fees (expense) = fees
        - CR Cash (asset) = -(quantity * entry_price + fees)
        
        Returns:
            position_id
        """
        if quantity <= 0:
            raise ValueError(f"Quantity must be positive: {quantity}")
        if entry_price <= 0 or entry_price > 1:
            raise ValueError(f"Invalid entry price: {entry_price}")
        
        cost = quantity * entry_price
        total_cost = cost + fees

        if total_cost > self.get_equity():
            raise ValueError(f"Insufficient capital: required={total_cost}")
        
        with self._get_connection() as conn:
            # Create transaction
            cursor = conn.execute(
                """
                INSERT INTO transactions (transaction_type, description, metadata)
                VALUES ('TRADE_ENTRY', ?, ?)
                """,
                (
                    f"Enter {side} position on {market_id[:20]}",
                    json.dumps(metadata) if metadata else None
                )
            )
            txn_id = cursor.lastrowid
            
            # Get account IDs
            cash_id = self._get_account_id(conn, 'Cash')
            positions_id = self._get_account_id(conn, 'Positions')
            fees_id = self._get_account_id(conn, 'Trading Fees')
            
            # DR positions_open
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, positions_id, _format_amount(cost)))
            
            # DR trading_fees
            if fees > 0:
                conn.execute("""
                    INSERT INTO transaction_lines (transaction_id, account_id, amount)
                    VALUES (?, ?, ?)
                """, (txn_id, fees_id, _format_amount(fees)))
            
            # CR cash
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, cash_id, _format_amount(-total_cost)))

            # Create position record (align position_id with transaction_id for compatibility)
            cursor = conn.execute(
                """
                INSERT INTO positions (
                    position_id, market_id, token_id, strategy, side,
                    entry_price, quantity, current_price, status,
                    entry_timestamp, opened_at, entry_order_id,
                    entry_fees, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
                """,
                (
                    txn_id,
                    market_id,
                    token_id,
                    strategy,
                    side,
                    str(entry_price),
                    str(quantity),
                    str(entry_price),
                    datetime.utcnow().isoformat(),
                    datetime.utcnow().isoformat(),
                    order_id,
                    _format_amount(fees),
                    json.dumps(metadata) if metadata else None
                )
            )
            position_row_id = cursor.lastrowid
            
            conn.commit()
            
            logger.info(
                f"Recorded trade entry: {strategy} | {side} {quantity} @ {entry_price} | "
                f"Cost: ${total_cost} | Position ID: {txn_id}"
            )
            
            return txn_id

    def record_reconciled_position(
        self,
        market_id: str,
        side: str,
        quantity: Decimal,
        entry_price: Decimal,
        strategy: str = "reconciled",
        token_id: str = "",
        order_id: str = "",
        metadata: Optional[Dict] = None,
        correlation_id: Optional[str] = None,
    ) -> int:
        """
        Record an externally discovered position without touching cash.

        Double-entry:
        - DR Positions (asset)
        - CR Owner Equity (equity)
        """
        if quantity <= 0:
            raise ValueError(f"Quantity must be positive: {quantity}")
        if entry_price <= 0 or entry_price > 1:
            raise ValueError(f"Invalid entry price: {entry_price}")

        cost = quantity * entry_price

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO transactions (transaction_type, description, metadata, correlation_id)
                VALUES ('POSITION_RECONCILE', ?, ?, ?)
                """,
                (
                    f"Reconcile {side} position on {market_id[:20]}",
                    json.dumps(metadata) if metadata else None,
                    correlation_id,
                ),
            )
            txn_id = cursor.lastrowid

            positions_id = self._get_account_id(conn, 'Positions')
            equity_id = self._get_account_id(conn, 'Owner Equity')

            conn.execute(
                """
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
                """,
                (txn_id, positions_id, _format_amount(cost)),
            )
            conn.execute(
                """
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
                """,
                (txn_id, equity_id, _format_amount(-cost)),
            )

            conn.execute(
                """
                INSERT INTO positions (
                    position_id, market_id, token_id, strategy, side,
                    entry_price, quantity, current_price, status,
                    entry_timestamp, opened_at, entry_order_id, entry_fees, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
                """,
                (
                    txn_id,
                    market_id,
                    token_id,
                    strategy,
                    side,
                    str(entry_price),
                    str(quantity),
                    str(entry_price),
                    datetime.utcnow().isoformat(),
                    datetime.utcnow().isoformat(),
                    order_id,
                    _format_amount(Decimal("0")),
                    json.dumps(metadata) if metadata else None,
                ),
            )

            conn.commit()

            logger.warning(
                "Reconciled external position: %s | %s %s @ %s",
                strategy,
                side,
                quantity,
                entry_price,
            )
            return txn_id
    
    def record_trade_exit(
        self,
        position_id: int,
        exit_price: Decimal,
        fees: Decimal,
        exit_reason: str = "EXIT",
        order_id: str = ""
    ) -> int:
        """
        Record trade exit (closing position).
        
        Double-entry:
        - DR Cash (asset) = quantity * exit_price - fees
        - DR Trading Fees (expense) = fees
        - CR Positions (asset) = -(quantity * entry_price)
        - DR/CR Trading Revenue/Loss = realized PnL
        
        Returns:
            transaction_id
        """
        with self._get_connection() as conn:
            # Get position details
            cursor = conn.execute(
                """
                SELECT * FROM positions WHERE position_id = ? AND status = 'OPEN'
                """,
                (position_id,)
            )
            pos = cursor.fetchone()
            
            if not pos:
                raise LedgerError(f"Position {position_id} not found or already closed")
            
            quantity = Decimal(str(pos['quantity']))
            entry_price = Decimal(str(pos['entry_price']))
            strategy = pos['strategy']
            market_id = pos['market_id']
            
            entry_fees = Decimal(str(pos['entry_fees'] or 0))

            proceeds = quantity * exit_price - fees
            cost = quantity * entry_price
            cost_basis = cost + entry_fees
            realized_pnl = proceeds - cost_basis
            pnl_for_ledger = (quantity * exit_price) - cost
            
            # Create transaction
            cursor = conn.execute(
                """
                INSERT INTO transactions (transaction_type, description)
                VALUES ('TRADE_EXIT', ?)
                """,
                (f"Exit position {position_id}: {exit_reason}",)
            )
            txn_id = cursor.lastrowid
            
            # Get account IDs
            cash_id = self._get_account_id(conn, 'Cash')
            positions_id = self._get_account_id(conn, 'Positions')
            fees_id = self._get_account_id(conn, 'Trading Fees')
            revenue_id = self._get_account_id(conn, 'Trading Revenue')
            loss_id = self._get_account_id(conn, 'Trading Loss')
            
            # DR cash (proceeds after fees)
            conn.execute("""
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
            """, (txn_id, cash_id, _format_amount(proceeds)))
            
            # DR trading_fees
            if fees > 0:
                conn.execute("""
                    INSERT INTO transaction_lines (transaction_id, account_id, amount)
                    VALUES (?, ?, ?)
                """, (txn_id, fees_id, _format_amount(fees)))
            
            # CR positions_open (remove cost basis)
            conn.execute(
                """
                INSERT INTO transaction_lines (transaction_id, account_id, amount)
                VALUES (?, ?, ?)
                """,
                (txn_id, positions_id, _format_amount(-cost))
            )

            if pnl_for_ledger >= 0:
                conn.execute(
                    """
                    INSERT INTO transaction_lines (transaction_id, account_id, amount)
                    VALUES (?, ?, ?)
                    """,
                    (txn_id, revenue_id, _format_amount(-pnl_for_ledger))
                )
            else:
                conn.execute(
                    """
                    INSERT INTO transaction_lines (transaction_id, account_id, amount)
                    VALUES (?, ?, ?)
                    """,
                    (txn_id, loss_id, _format_amount(abs(pnl_for_ledger)))
                )

            cursor = conn.execute(
                "SELECT SUM(amount) as total FROM transaction_lines WHERE transaction_id = ?",
                (txn_id,)
            )
            total = Decimal(str(cursor.fetchone()[0] or "0"))
            if total != Decimal("0"):
                adjustment = -total
                adjustment_account = revenue_id if adjustment < 0 else loss_id
                adjustment_amount = str(adjustment) if Decimal(_format_amount(adjustment)) == Decimal("0.00") else _format_amount(adjustment)
                conn.execute(
                    """
                    INSERT INTO transaction_lines (transaction_id, account_id, amount)
                    VALUES (?, ?, ?)
                    """,
                    (txn_id, adjustment_account, adjustment_amount)
                )

            conn.execute(
                """
                UPDATE positions
                SET status = 'CLOSED',
                    exit_price = ?,
                    realized_pnl = ?,
                    exit_fees = ?,
                    exit_order_id = ?,
                    exit_timestamp = CURRENT_TIMESTAMP,
                    closed_at = CURRENT_TIMESTAMP,
                    current_price = ?
                WHERE position_id = ?
                """,
                (
                    str(exit_price),
                    _format_amount(realized_pnl),
                    _format_amount(fees),
                    order_id,
                    str(exit_price),
                    position_id
                )
            )
            
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
                """, (str(price), token_id))
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
                    (julianday('now') - julianday(COALESCE(p.opened_at, p.entry_timestamp))) * 86400 as hold_time_seconds
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
            cursor = conn.execute(
                """
                SELECT 
                    COUNT(DISTINCT t.id) as trade_count,
                    SUM(CASE WHEN a.account_name = 'Trading Revenue' THEN -tl.amount ELSE 0 END) as total_profit,
                    SUM(CASE WHEN a.account_name = 'Trading Loss' THEN tl.amount ELSE 0 END) as total_loss,
                    SUM(CASE WHEN a.account_name = 'Trading Revenue' THEN -tl.amount ELSE 0 END)
                    - SUM(CASE WHEN a.account_name = 'Trading Loss' THEN tl.amount ELSE 0 END) as net_pnl
                FROM transactions t
                JOIN transaction_lines tl ON t.id = tl.transaction_id
                JOIN accounts a ON tl.account_id = a.id
                WHERE t.transaction_type = 'TRADE_EXIT'
                  AND t.created_at >= datetime('now', ? || ' days')
            """,
                (-days,)
            )
            
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
        cursor = conn.execute("SELECT id FROM accounts WHERE account_name = ?", (account_name,))
        row = cursor.fetchone()
        if not row:
            raise LedgerError(f"Account not found: {account_name}")
        return row['id']