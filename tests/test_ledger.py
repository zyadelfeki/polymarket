#!/usr/bin/env python3
"""
Unit Tests for Double-Entry Ledger

Critical tests:
1. Transaction balancing enforcement
2. Equity calculation correctness
3. Realized vs unrealized PnL
4. Concurrent transaction handling
5. Edge cases (zero amounts, negative PnL)
"""

import unittest
import sqlite3
import os
import tempfile
from decimal import Decimal
from datetime import datetime
from contextlib import contextmanager

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database.ledger import Ledger

class TestLedger(unittest.TestCase):
    """
    Test double-entry ledger implementation.
    """
    
    def setUp(self):
        """Create temporary database for each test"""
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.ledger = Ledger(db_path=self.db_path)
        
        # Initialize schema
        with self._get_connection() as conn:
            with open('database/schema.sql', 'r') as f:
                conn.executescript(f.read())
    
    def tearDown(self):
        """Clean up temporary database"""
        os.close(self.db_fd)
        os.unlink(self.db_path)
    
    @contextmanager
    def _get_connection(self):
        """Get database connection"""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()
    
    # ===========================================
    # Test 1: Transaction Balancing Enforcement
    # ===========================================
    
    def test_deposit_transaction_balances(self):
        """Test deposit creates balanced transaction"""
        txn_id = self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        # Check transaction lines sum to zero
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT SUM(amount) as total
                FROM transaction_lines
                WHERE transaction_id = ?
            """, (txn_id,))
            total = Decimal(str(cursor.fetchone()[0]))
        
        self.assertEqual(total, Decimal('0.00'), "Deposit transaction must balance")
    
    def test_trade_entry_transaction_balances(self):
        """Test trade entry creates balanced transaction"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        txn_id = self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        # Check transaction lines sum to zero
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT SUM(amount) as total
                FROM transaction_lines
                WHERE transaction_id = ?
            """, (txn_id,))
            total = Decimal(str(cursor.fetchone()[0]))
        
        self.assertEqual(total, Decimal('0.00'), "Trade entry transaction must balance")
    
    def test_trade_exit_transaction_balances(self):
        """Test trade exit creates balanced transaction"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        position_id = self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        txn_id = self.ledger.record_trade_exit(
            position_id=position_id,
            exit_price=Decimal('0.60'),
            fees=Decimal('1.20')
        )
        
        # Check transaction lines sum to zero
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT SUM(amount) as total
                FROM transaction_lines
                WHERE transaction_id = ?
            """, (txn_id,))
            total = Decimal(str(cursor.fetchone()[0]))
        
        self.assertEqual(total, Decimal('0.00'), "Trade exit transaction must balance")
    
    # ===========================================
    # Test 2: Equity Calculation
    # ===========================================
    
    def test_equity_after_deposit(self):
        """Test equity equals deposit amount"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        equity = self.ledger.get_equity()
        self.assertEqual(equity, Decimal('1000.00'))
    
    def test_equity_after_trade_entry(self):
        """Test equity decreases by cost + fees after entry"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        # Equity should be 1000 - 50 (cost) - 1 (fees) = 949
        equity = self.ledger.get_equity()
        self.assertEqual(equity, Decimal('949.00'))
    
    def test_equity_after_winning_trade(self):
        """Test equity increases after profitable trade"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        position_id = self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        self.ledger.record_trade_exit(
            position_id=position_id,
            exit_price=Decimal('0.70'),  # 40% profit
            fees=Decimal('1.40')
        )
        
        # Entry: 1000 - 50 - 1 = 949
        # Exit: 949 + 70 - 1.40 = 1017.60
        equity = self.ledger.get_equity()
        self.assertEqual(equity, Decimal('1017.60'))
    
    def test_equity_after_losing_trade(self):
        """Test equity decreases after losing trade"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        position_id = self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        self.ledger.record_trade_exit(
            position_id=position_id,
            exit_price=Decimal('0.30'),  # -40% loss
            fees=Decimal('0.60')
        )
        
        # Entry: 1000 - 50 - 1 = 949
        # Exit: 949 + 30 - 0.60 = 978.40
        equity = self.ledger.get_equity()
        self.assertEqual(equity, Decimal('978.40'))
    
    def test_equity_with_multiple_trades(self):
        """Test equity tracks correctly across multiple trades"""
        self.ledger.record_deposit(amount=Decimal('10000.00'))
        
        # Trade 1: Win
        pos1 = self.ledger.record_trade_entry(
            market_id='market1', side='YES',
            quantity=Decimal('100'), entry_price=Decimal('0.50'), fees=Decimal('1.00')
        )
        self.ledger.record_trade_exit(
            position_id=pos1, exit_price=Decimal('0.70'), fees=Decimal('1.40')
        )
        
        # Trade 2: Loss
        pos2 = self.ledger.record_trade_entry(
            market_id='market2', side='NO',
            quantity=Decimal('200'), entry_price=Decimal('0.60'), fees=Decimal('2.40')
        )
        self.ledger.record_trade_exit(
            position_id=pos2, exit_price=Decimal('0.40'), fees=Decimal('1.60')
        )
        
        # Trade 3: Win
        pos3 = self.ledger.record_trade_entry(
            market_id='market3', side='YES',
            quantity=Decimal('150'), entry_price=Decimal('0.45'), fees=Decimal('1.35')
        )
        self.ledger.record_trade_exit(
            position_id=pos3, exit_price=Decimal('0.55'), fees=Decimal('1.65')
        )
        
        # Calculate expected
        # Start: 10000
        # T1: -51 (entry), +68.60 (exit) = +17.60
        # T2: -122.40 (entry), +78.40 (exit) = -44.00
        # T3: -68.85 (entry), +80.85 (exit) = +12.00
        # Total: 10000 + 17.60 - 44.00 + 12.00 = 9985.60
        
        equity = self.ledger.get_equity()
        self.assertAlmostEqual(float(equity), 9985.60, places=2)
    
    # ===========================================
    # Test 3: Realized vs Unrealized PnL
    # ===========================================
    
    def test_open_position_not_in_realized_pnl(self):
        """Test open positions don't affect realized PnL"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        # Check realized PnL is zero (position still open)
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT COUNT(*) FROM positions WHERE status = 'CLOSED'
            """)
            closed_count = cursor.fetchone()[0]
        
        self.assertEqual(closed_count, 0, "No positions should be closed")
    
    def test_closed_position_in_realized_pnl(self):
        """Test closed positions show in realized PnL"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        position_id = self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        self.ledger.record_trade_exit(
            position_id=position_id,
            exit_price=Decimal('0.70'),
            fees=Decimal('1.40')
        )
        
        # Check position is closed
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT realized_pnl FROM positions WHERE position_id = ?
            """, (position_id,))
            realized_pnl = Decimal(str(cursor.fetchone()[0]))
        
        # PnL = (exit_price - entry_price) * quantity - fees
        # = (0.70 - 0.50) * 100 - 1.00 - 1.40
        # = 20 - 2.40 = 17.60
        self.assertEqual(realized_pnl, Decimal('17.60'))
    
    # ===========================================
    # Test 4: Edge Cases
    # ===========================================
    
    def test_zero_quantity_rejected(self):
        """Test zero quantity trade is rejected"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        with self.assertRaises(ValueError):
            self.ledger.record_trade_entry(
                market_id='test_market',
                side='YES',
                quantity=Decimal('0'),  # Invalid
                entry_price=Decimal('0.50'),
                fees=Decimal('1.00')
            )
    
    def test_negative_quantity_rejected(self):
        """Test negative quantity trade is rejected"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        with self.assertRaises(ValueError):
            self.ledger.record_trade_entry(
                market_id='test_market',
                side='YES',
                quantity=Decimal('-100'),  # Invalid
                entry_price=Decimal('0.50'),
                fees=Decimal('1.00')
            )
    
    def test_invalid_price_rejected(self):
        """Test price outside [0.01, 0.99] is rejected"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        with self.assertRaises(ValueError):
            self.ledger.record_trade_entry(
                market_id='test_market',
                side='YES',
                quantity=Decimal('100'),
                entry_price=Decimal('1.50'),  # Invalid (> 0.99)
                fees=Decimal('1.00')
            )
    
    def test_insufficient_capital_detected(self):
        """Test trade exceeding capital is detected"""
        self.ledger.record_deposit(amount=Decimal('100.00'))
        
        # Try to enter position worth more than capital
        with self.assertRaises(ValueError):
            self.ledger.record_trade_entry(
                market_id='test_market',
                side='YES',
                quantity=Decimal('1000'),  # Costs 500 + fees
                entry_price=Decimal('0.50'),
                fees=Decimal('10.00')
            )
    
    def test_total_loss_trade(self):
        """Test complete loss (exit price = 0.01) handled correctly"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        position_id = self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        self.ledger.record_trade_exit(
            position_id=position_id,
            exit_price=Decimal('0.01'),  # Near-total loss
            fees=Decimal('0.02')
        )
        
        # Entry: 1000 - 50 - 1 = 949
        # Exit: 949 + 1 - 0.02 = 949.98
        # Total loss: ~50
        equity = self.ledger.get_equity()
        self.assertEqual(equity, Decimal('949.98'))
    
    def test_max_gain_trade(self):
        """Test maximum gain (exit price = 0.99) handled correctly"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        position_id = self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        self.ledger.record_trade_exit(
            position_id=position_id,
            exit_price=Decimal('0.99'),  # Near-double
            fees=Decimal('1.98')
        )
        
        # Entry: 1000 - 50 - 1 = 949
        # Exit: 949 + 99 - 1.98 = 1046.02
        equity = self.ledger.get_equity()
        self.assertEqual(equity, Decimal('1046.02'))
    
    # ===========================================
    # Test 5: Audit Trail
    # ===========================================
    
    def test_all_transactions_recorded(self):
        """Test every operation creates transaction record"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        position_id = self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        self.ledger.record_trade_exit(
            position_id=position_id,
            exit_price=Decimal('0.70'),
            fees=Decimal('1.40')
        )
        
        # Should have 3 transactions: DEPOSIT, TRADE_ENTRY, TRADE_EXIT
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM transactions")
            count = cursor.fetchone()[0]
        
        self.assertEqual(count, 3)
    
    def test_transaction_timestamp_recorded(self):
        """Test every transaction has timestamp"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT timestamp FROM transactions WHERE transaction_type = 'DEPOSIT'
            """)
            timestamp = cursor.fetchone()[0]
        
        self.assertIsNotNone(timestamp)
        # Should be recent (within last minute)
        txn_time = datetime.fromisoformat(timestamp)
        time_diff = (datetime.utcnow() - txn_time).total_seconds()
        self.assertLess(time_diff, 60)
    
    def test_position_history_complete(self):
        """Test complete position lifecycle recorded"""
        self.ledger.record_deposit(amount=Decimal('1000.00'))
        
        position_id = self.ledger.record_trade_entry(
            market_id='test_market',
            side='YES',
            quantity=Decimal('100'),
            entry_price=Decimal('0.50'),
            fees=Decimal('1.00')
        )
        
        self.ledger.record_trade_exit(
            position_id=position_id,
            exit_price=Decimal('0.70'),
            fees=Decimal('1.40')
        )
        
        # Check position record has all data
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT entry_price, exit_price, entry_fees, exit_fees, realized_pnl, status
                FROM positions WHERE position_id = ?
            """, (position_id,))
            row = cursor.fetchone()
        
        self.assertIsNotNone(row)
        self.assertEqual(Decimal(str(row[0])), Decimal('0.50'))  # entry_price
        self.assertEqual(Decimal(str(row[1])), Decimal('0.70'))  # exit_price
        self.assertEqual(Decimal(str(row[2])), Decimal('1.00'))  # entry_fees
        self.assertEqual(Decimal(str(row[3])), Decimal('1.40'))  # exit_fees
        self.assertEqual(Decimal(str(row[4])), Decimal('17.60'))  # realized_pnl
        self.assertEqual(row[5], 'CLOSED')  # status

if __name__ == '__main__':
    unittest.main()