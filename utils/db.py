"""
Database Layer
SQLite storage for trade history and performance tracking
"""
import sqlite3
from typing import Dict, List, Optional
from datetime import datetime
from pathlib import Path
import logging
import json

logger = logging.getLogger(__name__)

class Database:
    """SQLite database manager"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = None
        self.init_db()
    
    def init_db(self):
        """Create tables if not exist"""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = self.conn.cursor()
        
        # Trades table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                market_id TEXT NOT NULL,
                question TEXT,
                symbol TEXT,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                size REAL NOT NULL,
                profit REAL,
                roi REAL,
                confidence REAL,
                strategy TEXT,
                reason TEXT,
                status TEXT DEFAULT 'OPEN'
            )
        ''')
        
        # Performance snapshots
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                capital REAL NOT NULL,
                total_return_pct REAL,
                win_rate REAL,
                sharpe_ratio REAL,
                total_trades INTEGER,
                open_positions INTEGER
            )
        ''')
        
        # Volatility events
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS volatility_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                volatility_pct REAL NOT NULL,
                price REAL NOT NULL,
                opportunities_found INTEGER
            )
        ''')
        
        self.conn.commit()
        logger.info(f"✅ Database initialized: {self.db_path}")
    
    def log_trade(self, trade: Dict):
        """Insert trade record"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO trades (
                timestamp, market_id, question, symbol, side, entry_price,
                exit_price, size, profit, roi, confidence, strategy, reason, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade.get('timestamp', datetime.utcnow().isoformat()),
            trade['market_id'],
            trade.get('question'),
            trade.get('symbol'),
            trade['side'],
            trade['entry_price'],
            trade.get('exit_price'),
            float(trade['size']),
            float(trade.get('profit', 0)),
            trade.get('roi'),
            trade.get('confidence'),
            trade.get('strategy'),
            trade.get('reason'),
            trade.get('status', 'OPEN')
        ))
        self.conn.commit()
    
    def log_snapshot(self, stats: Dict):
        """Insert performance snapshot"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO snapshots (
                timestamp, capital, total_return_pct, win_rate,
                sharpe_ratio, total_trades, open_positions
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.utcnow().isoformat(),
            stats['current_capital'],
            stats['total_return_pct'],
            stats['win_rate_pct'],
            stats['sharpe_ratio'],
            stats['total_trades'],
            stats.get('open_positions', 0)
        ))
        self.conn.commit()
    
    def close(self):
        if self.conn:
            self.conn.close()