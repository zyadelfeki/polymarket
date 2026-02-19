#!/usr/bin/env python3
"""
Institutional-Grade Async Ledger Manager

Features:
- Full async/await (no event loop blocking)
- Connection pooling
- TTL caching for hot paths (equity queries)
- Prepared statements (SQL injection safe)
- Transaction batching
- Comprehensive error handling
- Metrics tracking
- Database health monitoring
- Automatic schema initialization with fallback

Standards:
- Double-entry accounting enforced
- ACID transactions
- Audit trail complete
- Zero data loss
"""

import aiosqlite
import asyncio
import os
import json
from typing import List, Dict, Optional, Tuple, Any
from decimal import Decimal
from datetime import datetime, timedelta
from dataclasses import dataclass
from contextlib import asynccontextmanager
from utils.decimal_json import dumps as decimal_dumps
from services.correlation_context import inject_correlation
try:
    from cachetools import TTLCache
    _cachetools_available = True
except ImportError:
    _cachetools_available = False

    class TTLCache(dict):
        """Minimal TTL cache fallback when cachetools is unavailable."""

        def __init__(self, maxsize: int = 128, ttl: float = 600.0):
            super().__init__()
            self.maxsize = maxsize
            self.ttl = ttl
            self._expires = {}

        def _purge_expired(self):
            now = time.time()
            expired = [k for k, exp in self._expires.items() if exp <= now]
            for k in expired:
                self._expires.pop(k, None)
                super().pop(k, None)

        def __setitem__(self, key, value):
            self._purge_expired()
            if len(self._expires) >= self.maxsize:
                oldest = min(self._expires.items(), key=lambda item: item[1])[0]
                self._expires.pop(oldest, None)
                super().pop(oldest, None)
            self._expires[key] = time.time() + self.ttl
            return super().__setitem__(key, value)

        def __getitem__(self, key):
            self._purge_expired()
            return super().__getitem__(key)

        def get(self, key, default=None):
            self._purge_expired()
            return super().get(key, default)

        def pop(self, key, default=None):
            self._expires.pop(key, None)
            return super().pop(key, default)

        def clear(self):
            self._expires.clear()
            return super().clear()

try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False
import time

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    import logging

    logging.basicConfig(level=logging.INFO)
    class _FallbackLogger:
        def __init__(self, name: str):
            self._logger = logging.getLogger(name)

        def _log(self, level, event: str, **kwargs):
            exc_info = kwargs.pop("exc_info", None)
            kwargs = inject_correlation(kwargs)
            message = f"{event} | {kwargs}" if kwargs else event
            self._logger.log(level, message, exc_info=exc_info)

        def debug(self, event: str, **kwargs):
            self._log(logging.DEBUG, event, **kwargs)

        def info(self, event: str, **kwargs):
            self._log(logging.INFO, event, **kwargs)

        def warning(self, event: str, **kwargs):
            self._log(logging.WARNING, event, **kwargs)

        def error(self, event: str, **kwargs):
            self._log(logging.ERROR, event, **kwargs)

    logger = _FallbackLogger(__name__)

# Embedded schema as fallback
EMBEDDED_SCHEMA = """
-- Polymarket Trading System Database Schema
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL UNIQUE,
    account_type TEXT NOT NULL CHECK(account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')),
    balance DECIMAL(20, 8) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO accounts (account_name, account_type, balance) VALUES
    ('Cash', 'ASSET', 0),
    ('Positions', 'ASSET', 0),
    ('Unrealized PnL', 'ASSET', 0),
    ('Trading Fees', 'EXPENSE', 0),
    ('Owner Equity', 'EQUITY', 0),
    ('Trading Revenue', 'REVENUE', 0),
    ('Trading Loss', 'EXPENSE', 0);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    transaction_type TEXT,
    strategy TEXT,
    reference_id TEXT,
    correlation_id TEXT,
    metadata TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transaction_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    amount DECIMAL(20, 8) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_transaction_lines_txn ON transaction_lines(transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_lines_account ON transaction_lines(account_id);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    entry_price DECIMAL(10, 8) NOT NULL,
    quantity DECIMAL(20, 8) NOT NULL,
    current_price DECIMAL(10, 8),
    exit_price DECIMAL(10, 8),
    unrealized_pnl DECIMAL(20, 8) DEFAULT 0,
    realized_pnl DECIMAL(20, 8) DEFAULT 0,
    fees DECIMAL(20, 8) DEFAULT 0,
    entry_fees DECIMAL(20, 8) DEFAULT 0,
    exit_fees DECIMAL(20, 8) DEFAULT 0,
    status TEXT DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'CLOSED')),
    entry_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    exit_timestamp TIMESTAMP,
    entry_order_id TEXT,
    exit_order_id TEXT,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy);
CREATE INDEX IF NOT EXISTS idx_positions_entry_time ON positions(entry_timestamp);

CREATE TRIGGER IF NOT EXISTS trg_update_account_balance_insert
AFTER INSERT ON transaction_lines
BEGIN
    UPDATE accounts
    SET balance = balance + NEW.amount,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.account_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_update_account_balance_delete
AFTER DELETE ON transaction_lines
BEGIN
    UPDATE accounts
    SET balance = balance - OLD.amount,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = OLD.account_id;
END;

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    old_state TEXT,
    new_state TEXT,
    reason TEXT,
    context TEXT,
    correlation_id TEXT,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_log_time ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS idempotency_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    order_id TEXT,
    correlation_id TEXT,
    status TEXT,
    filled_quantity DECIMAL(20, 8) DEFAULT 0,
    filled_price DECIMAL(10, 8) DEFAULT 0,
    fees DECIMAL(20, 8) DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_idempotency_key ON idempotency_log(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_idempotency_order ON idempotency_log(order_id);

CREATE TABLE IF NOT EXISTS order_tracking (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       TEXT    NOT NULL UNIQUE,
    market_id      TEXT    NOT NULL,
    token_id       TEXT    NOT NULL DEFAULT '',
    outcome        TEXT    NOT NULL,
    side           TEXT    NOT NULL DEFAULT 'BUY',
    size           TEXT    NOT NULL,
    price          TEXT    NOT NULL,
    order_state    TEXT    NOT NULL DEFAULT 'CREATED',
    opened_at      TEXT    NOT NULL,
    closed_at      TEXT,
    pnl            TEXT,
    charlie_p_win  TEXT,
    charlie_conf   TEXT,
    charlie_regime TEXT,
    strategy       TEXT,
    model_votes    TEXT,
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_order_tracking_state  ON order_tracking(order_state);
CREATE INDEX IF NOT EXISTS idx_order_tracking_market ON order_tracking(market_id);
CREATE INDEX IF NOT EXISTS idx_order_tracking_opened ON order_tracking(opened_at);
"""


@dataclass
class PositionData:
    """Position data structure"""
    id: int
    market_id: str
    token_id: str
    strategy: str
    entry_price: Decimal
    quantity: Decimal
    current_price: Optional[Decimal]
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    status: str
    entry_timestamp: datetime
    exit_timestamp: Optional[datetime]
    hold_time_seconds: float


@dataclass
class AccountBalance:
    """Account balance data"""
    account_id: int
    account_name: str
    account_type: str
    balance: Decimal


class ConnectionPool:
    """
    Async SQLite connection pool.
    
    Maintains multiple database connections to allow concurrent queries
    without blocking.
    """
    
    def __init__(self, db_path: str, pool_size: int = 5):
        self.db_path = db_path
        self.pool_size = pool_size
        self.connections: asyncio.Queue = asyncio.Queue(maxsize=pool_size)
        self._initialized = False
        self.lock = asyncio.Lock()
        
        # CRITICAL: Ensure database directory exists
        if self.db_path not in (':memory:', '') and not self.db_path.startswith('file:'):
            db_dir = os.path.dirname(self.db_path)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)
                logger.info("database_directory_created", path=db_dir)
    
    async def initialize(self):
        """Initialize connection pool and create schema if needed."""
        if self._initialized:
            return
        
        async with self.lock:
            if self._initialized:  # Double-check
                return
            
            # Try to load schema from file, fallback to embedded
            schema_sql = None
            schema_source = "embedded"
            
            try:
                schema_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    'schema.sql'
                )
                
                if os.path.exists(schema_path):
                    with open(schema_path, 'r') as f:
                        schema_sql = f.read()
                    schema_source = "file"
                    logger.info(
                        "schema_loaded_from_file",
                        schema_path=schema_path,
                        schema_size=len(schema_sql)
                    )
                else:
                    logger.warning(
                        "schema_file_not_found",
                        schema_path=schema_path,
                        fallback="using_embedded_schema"
                    )
                    schema_sql = EMBEDDED_SCHEMA
            
            except Exception as e:
                logger.warning(
                    "schema_file_load_error",
                    error=str(e),
                    fallback="using_embedded_schema"
                )
                schema_sql = EMBEDDED_SCHEMA
            
            # Use embedded schema if nothing loaded
            if not schema_sql:
                schema_sql = EMBEDDED_SCHEMA
                schema_source = "embedded"
            
            logger.info(
                "initializing_database_schema",
                schema_source=schema_source,
                db_path=self.db_path
            )
            
            # CRITICAL FIX: Create temporary connection for schema initialization
            # This ensures schema is visible to all subsequent connections
            temp_conn = await aiosqlite.connect(
                self.db_path,
                isolation_level=None
            )
            
            try:
                # Enable WAL mode and foreign keys
                await temp_conn.execute("PRAGMA foreign_keys = ON")
                await temp_conn.execute("PRAGMA journal_mode = WAL")
                
                # Execute schema
                await temp_conn.executescript(schema_sql)
                await temp_conn.commit()

                # Ensure accounts table has updated_at for triggers
                cursor = await temp_conn.execute("PRAGMA table_info(accounts)")
                columns = [row[1] for row in await cursor.fetchall()]
                if "updated_at" not in columns:
                    await temp_conn.execute(
                        "ALTER TABLE accounts ADD COLUMN updated_at TIMESTAMP"
                    )
                    await temp_conn.execute(
                        "UPDATE accounts SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
                    )

                cursor = await temp_conn.execute("PRAGMA table_info(positions)")
                position_columns = [row[1] for row in await cursor.fetchall()]
                if "side" not in position_columns:
                    await temp_conn.execute("ALTER TABLE positions ADD COLUMN side TEXT")
                if "entry_order_id" not in position_columns:
                    await temp_conn.execute("ALTER TABLE positions ADD COLUMN entry_order_id TEXT")
                if "exit_order_id" not in position_columns:
                    await temp_conn.execute("ALTER TABLE positions ADD COLUMN exit_order_id TEXT")
                if "position_id" not in position_columns:
                    await temp_conn.execute(
                        "ALTER TABLE positions ADD COLUMN position_id INTEGER"
                    )
                    await temp_conn.execute(
                        "UPDATE positions SET position_id = id WHERE position_id IS NULL"
                    )
                    await temp_conn.execute(
                        """
                        CREATE TRIGGER IF NOT EXISTS trg_positions_set_position_id
                        AFTER INSERT ON positions
                        WHEN NEW.position_id IS NULL
                        BEGIN
                            UPDATE positions SET position_id = NEW.id WHERE id = NEW.id;
                        END;
                        """
                    )
                    await temp_conn.commit()

                cursor = await temp_conn.execute("PRAGMA table_info(transactions)")
                transaction_columns = [row[1] for row in await cursor.fetchall()]
                if "transaction_type" not in transaction_columns:
                    await temp_conn.execute("ALTER TABLE transactions ADD COLUMN transaction_type TEXT")
                if "strategy" not in transaction_columns:
                    await temp_conn.execute("ALTER TABLE transactions ADD COLUMN strategy TEXT")
                if "reference_id" not in transaction_columns:
                    await temp_conn.execute("ALTER TABLE transactions ADD COLUMN reference_id TEXT")
                if "correlation_id" not in transaction_columns:
                    await temp_conn.execute("ALTER TABLE transactions ADD COLUMN correlation_id TEXT")
                if "metadata" not in transaction_columns:
                    await temp_conn.execute("ALTER TABLE transactions ADD COLUMN metadata TEXT")
                if "timestamp" not in transaction_columns:
                    await temp_conn.execute("ALTER TABLE transactions ADD COLUMN timestamp TIMESTAMP")
                await temp_conn.commit()

                cursor = await temp_conn.execute("PRAGMA table_info(audit_log)")
                audit_columns = [row[1] for row in await cursor.fetchall()]
                if "entity_id" not in audit_columns:
                    await temp_conn.execute("ALTER TABLE audit_log ADD COLUMN entity_id TEXT")
                if "old_state" not in audit_columns:
                    await temp_conn.execute("ALTER TABLE audit_log ADD COLUMN old_state TEXT")
                if "new_state" not in audit_columns:
                    await temp_conn.execute("ALTER TABLE audit_log ADD COLUMN new_state TEXT")
                if "reason" not in audit_columns:
                    await temp_conn.execute("ALTER TABLE audit_log ADD COLUMN reason TEXT")
                if "context" not in audit_columns:
                    await temp_conn.execute("ALTER TABLE audit_log ADD COLUMN context TEXT")
                if "correlation_id" not in audit_columns:
                    await temp_conn.execute("ALTER TABLE audit_log ADD COLUMN correlation_id TEXT")
                if "details" not in audit_columns:
                    await temp_conn.execute("ALTER TABLE audit_log ADD COLUMN details TEXT")
                await temp_conn.commit()

                cursor = await temp_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='idempotency_log'"
                )
                if not await cursor.fetchone():
                    await temp_conn.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS idempotency_log (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            idempotency_key TEXT NOT NULL UNIQUE,
                            order_id TEXT,
                            correlation_id TEXT,
                            status TEXT,
                            filled_quantity DECIMAL(20, 8) DEFAULT 0,
                            filled_price DECIMAL(10, 8) DEFAULT 0,
                            fees DECIMAL(20, 8) DEFAULT 0,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_idempotency_key ON idempotency_log(idempotency_key);
                        CREATE INDEX IF NOT EXISTS idx_idempotency_order ON idempotency_log(order_id);
                        """
                    )
                    await temp_conn.commit()
                
                # CRITICAL: Force a checkpoint to ensure WAL is applied to main DB
                # This makes schema visible to all new connections
                await temp_conn.execute("PRAGMA wal_checkpoint(FULL)")
                await temp_conn.commit()
                
                # Verify tables exist
                cursor = await temp_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
                tables = await cursor.fetchall()
                table_names = [t[0] for t in tables]

                if 'accounts' not in table_names:
                    raise RuntimeError("Critical: 'accounts' table not created!")
                if 'transactions' not in table_names:
                    raise RuntimeError("Critical: 'transactions' table not created!")

                logger.info(
                    "database_schema_initialized",
                    db_path=self.db_path,
                    schema_source=schema_source,
                    tables=table_names
                )
            
            except Exception as e:
                logger.error(
                    "schema_initialization_failed",
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True
                )
                await temp_conn.close()
                raise
            
            finally:
                # Close temporary connection - don't add to pool
                await temp_conn.close()
            
            # CRITICAL FIX: Small delay to ensure filesystem sync (especially on Windows)
            await asyncio.sleep(0.1)
            
            # NOW create pool connections - schema is guaranteed visible
            logger.info("creating_connection_pool", pool_size=self.pool_size)
            
            for i in range(self.pool_size):
                conn = await aiosqlite.connect(
                    self.db_path,
                    isolation_level=None
                )
                await conn.execute("PRAGMA foreign_keys = ON")
                await conn.execute("PRAGMA journal_mode = WAL")
                
                # Verify this connection can see tables
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                )
                table_count = (await cursor.fetchone())[0]
                
                if table_count == 0:
                    await conn.close()
                    raise RuntimeError(
                        f"Connection {i} cannot see database tables! "
                        "This indicates a critical race condition."
                    )
                
                await self.connections.put(conn)
                logger.debug(f"connection_{i}_added", table_count=table_count)
            
            self._initialized = True
            logger.info(
                "connection_pool_initialized",
                db_path=self.db_path,
                pool_size=self.pool_size
            )
    
    async def acquire(self) -> aiosqlite.Connection:
        """Acquire connection from pool."""
        if not self._initialized:
            await self.initialize()
        return await self.connections.get()
    
    async def release(self, conn: aiosqlite.Connection):
        """Release connection back to pool."""
        await self.connections.put(conn)
    
    async def close_all(self):
        """Close all connections."""
        while not self.connections.empty():
            conn = await self.connections.get()
            await conn.close()
        
        logger.info("connection_pool_closed")


class AsyncLedger:
    """
    Production-grade async ledger manager.
    
    Implements double-entry accounting with full async support.
    
    Key features:
    - Non-blocking database operations
    - Connection pooling for concurrency
    - TTL caching for hot queries
    - Prepared statements everywhere
    - Transaction batching support
    - Comprehensive metrics
    - Automatic schema initialization with embedded fallback
    """
    
    def __init__(
        self,
        db_path: str = "data/trading.db",
        pool_size: int = 5,
        cache_ttl: int = 5
    ):
        """
        Initialize async ledger.
        
        Args:
            db_path: Path to SQLite database
            pool_size: Number of connections in pool
            cache_ttl: Cache TTL in seconds
        """
        # CRITICAL: Ensure database directory exists before anything else
        if db_path not in (':memory:', '') and not db_path.startswith('file:'):
            db_dir = os.path.dirname(db_path)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)
                logger.info("database_directory_created", path=db_dir)

        self.db_path = db_path
        self.pool = ConnectionPool(db_path, pool_size)

        # Caches
        self.equity_cache = TTLCache(maxsize=1, ttl=cache_ttl)
        self.position_cache = TTLCache(maxsize=100, ttl=cache_ttl)

        # Metrics
        self.queries_executed = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.total_query_time_ms = 0.0
        self._write_lock = asyncio.Lock()

        logger.info(
            "async_ledger_initialized",
            db_path=db_path,
            pool_size=pool_size,
            cache_ttl=cache_ttl
        )

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

    DECIMAL_COLUMNS = {
        "balance",
        "amount",
        "entry_price",
        "exit_price",
        "current_price",
        "filled_price",
        "filled_quantity",
        "quantity",
        "fees",
        "entry_fees",
        "exit_fees",
        "unrealized_pnl",
        "realized_pnl",
    }

    def _convert_row(self, row: Tuple, columns: List[str]) -> Tuple:
        converted: List[Any] = []
        for idx, value in enumerate(row):
            column = columns[idx]
            if value is not None and column in self.DECIMAL_COLUMNS:
                converted.append(Decimal(str(value)))
            else:
                converted.append(value)
        return tuple(converted)
    
    async def initialize(self):
        """Explicitly initialize database schema and connection pool."""
        await self.pool.initialize()
        logger.info("async_ledger_ready")
    
    async def _execute_query(
        self,
        query: str,
        params: Tuple = (),
        fetch_one: bool = False,
        fetch_all: bool = False,
        commit: bool = False
    ):
        """
        Execute database query with metrics.
        
        Args:
            query: SQL query
            params: Query parameters
            fetch_one: Return one row
            fetch_all: Return all rows
            commit: Commit transaction
        
        Returns:
            Query result or None
        """
        conn = await self.pool.acquire()
        
        try:
            start_time = time.time()
            
            cursor = await conn.execute(query, params)
            
            result = None
            if fetch_one:
                result = await cursor.fetchone()
                if result and cursor.description:
                    columns = [col[0] for col in cursor.description]
                    result = self._convert_row(result, columns)
            elif fetch_all:
                result = await cursor.fetchall()
                if result and cursor.description:
                    columns = [col[0] for col in cursor.description]
                    result = [self._convert_row(row, columns) for row in result]
            
            if commit:
                await conn.commit()
            
            # Metrics
            query_time_ms = (time.time() - start_time) * 1000
            self.queries_executed += 1
            self.total_query_time_ms += query_time_ms
            
            if query_time_ms > 100:  # Log slow queries
                logger.warning(
                    "slow_query",
                    query=query[:100],
                    time_ms=query_time_ms
                )
            
            return result
        
        except Exception as e:
            logger.error(
                "query_failed",
                error=str(e),
                query=query[:100]
            )
            raise
        
        finally:
            await self.pool.release(conn)

    async def execute(
        self,
        query: str,
        params: Tuple = (),
        fetch_one: bool = False,
        fetch_all: bool = False,
        commit: bool = False
    ):
        return await self._execute_query(
            query,
            params=params,
            fetch_one=fetch_one,
            fetch_all=fetch_all,
            commit=commit
        )

    async def execute_scalar(self, query: str, params: Tuple = ()) -> Any:
        result = await self._execute_query(query, params=params, fetch_one=True)
        if not result:
            return None
        return result[0]

    async def record_audit_event(
        self,
        *,
        entity_type: str,
        entity_id: Optional[str],
        old_state: Optional[str],
        new_state: Optional[str],
        reason: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> None:
        payload = context or {}
        details_json = None
        if payload:
            details_json = decimal_dumps(payload)

        await self._execute_query(
            """
            INSERT INTO audit_log
            (operation, entity_type, entity_id, old_state, new_state, reason, context, correlation_id, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "STATE_CHANGE",
                entity_type,
                entity_id,
                old_state,
                new_state,
                reason,
                details_json,
                correlation_id,
                details_json,
            ),
            commit=True,
        )

    async def get_idempotency_record(self, idempotency_key: str) -> Optional[Dict]:
        """
        Query idempotency_log table for existing order with this key.

        Returns:
            Dict with order_id, status, filled_quantity, filled_price, fees if found
            None if not found
        """
        row = await self.execute(
            """
            SELECT order_id, status, filled_quantity, filled_price, fees, correlation_id
            FROM idempotency_log
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
            fetch_one=True
        )

        if not row:
            return None

        return {
            "order_id": row[0],
            "status": row[1],
            "filled_quantity": row[2] if isinstance(row[2], Decimal) else Decimal(str(row[2])),
            "filled_price": row[3] if isinstance(row[3], Decimal) else Decimal(str(row[3])),
            "fees": row[4] if isinstance(row[4], Decimal) else Decimal(str(row[4])),
            "correlation_id": row[5],
        }

    async def record_idempotency(
        self,
        idempotency_key: str,
        order_id: str,
        correlation_id: str,
        status: str = "PENDING"
    ) -> None:
        """Record order in idempotency_log."""
        await self.execute(
            """
            INSERT OR IGNORE INTO idempotency_log (
                idempotency_key, order_id, correlation_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (idempotency_key, order_id, correlation_id, status),
            commit=True
        )

    async def update_idempotency(
        self,
        idempotency_key: str,
        status: str,
        filled_quantity: Decimal,
        filled_price: Decimal,
        fees: Decimal
    ) -> None:
        """Update idempotency record with fill details."""
        await self.execute(
            """
            UPDATE idempotency_log
            SET status = ?,
                filled_quantity = ?,
                filled_price = ?,
                fees = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE idempotency_key = ?
            """,
            (str(status), str(filled_quantity), str(filled_price), str(fees), idempotency_key),
            commit=True
        )

    async def record_trade_entry(
        self,
        order_id: str,
        market_id: str,
        token_id: str,
        strategy: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        correlation_id: str,
        **kwargs
    ) -> int:
        """Record trade with double-entry accounting (SQLite version)."""
        if "entry_price" in kwargs:
            price = kwargs.get("entry_price")
        if "metadata" in kwargs:
            metadata = kwargs.get("metadata")
        else:
            metadata = None

        position_value = Decimal(str(quantity)) * Decimal(str(price))
        if quantity <= 0:
            raise ValueError(f"Quantity must be positive: {quantity}")
        if Decimal(str(price)) < Decimal("0.001") or Decimal(str(price)) > Decimal("0.999"):
            raise ValueError(f"Invalid entry price: {price}")

        async with self._write_lock:
            conn = await self.pool.acquire()
            try:
                await conn.execute("BEGIN TRANSACTION")

                cursor = await conn.execute(
                    """
                    INSERT INTO transactions (description, transaction_type, strategy, reference_id, timestamp)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (f"Trade {side} {quantity} @ {price}", "TRADE", strategy, order_id)
                )
                tx_id = cursor.lastrowid

                cursor = await conn.execute(
                    "SELECT id FROM accounts WHERE account_name = 'Positions' LIMIT 1"
                )
                positions_account = (await cursor.fetchone())[0]

                cursor = await conn.execute(
                    "SELECT id FROM accounts WHERE account_name = 'Cash' LIMIT 1"
                )
                cash_account = (await cursor.fetchone())[0]

                await conn.execute(
                    "INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (?, ?, ?)",
                    (tx_id, positions_account, str(position_value))
                )

                await conn.execute(
                    "INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (?, ?, ?)",
                    (tx_id, cash_account, str(-position_value))
                )

                metadata_payload = {"correlation_id": correlation_id}
                if metadata is not None:
                    metadata_payload["metadata"] = metadata

                cursor = await conn.execute(
                    """
                    INSERT INTO positions
                    (market_id, token_id, strategy, side, entry_price, quantity, status,
                     entry_timestamp, entry_order_id, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, 'OPEN', CURRENT_TIMESTAMP, ?, ?)
                    """,
                    (
                        market_id,
                        token_id,
                        strategy,
                        side,
                        str(price),
                        str(quantity),
                        order_id,
                        decimal_dumps(metadata_payload) if correlation_id or metadata is not None else None
                    )
                )
                position_id = cursor.lastrowid

                await conn.execute(
                    """
                    INSERT INTO audit_log
                    (operation, entity_type, entity_id, old_state, new_state, reason, context, correlation_id, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "STATE_CHANGE",
                        "position",
                        str(position_id),
                        None,
                        "OPEN",
                        "trade_entry",
                        decimal_dumps({
                            "market_id": market_id,
                            "token_id": token_id,
                            "quantity": quantity,
                            "price": price,
                            "strategy": strategy,
                        }),
                        correlation_id,
                        decimal_dumps({
                            "market_id": market_id,
                            "token_id": token_id,
                            "quantity": quantity,
                            "price": price,
                            "strategy": strategy,
                        })
                    )
                )

                await conn.execute(
                    "INSERT INTO audit_log (operation, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
                    (
                        "CREATE",
                        "TRANSACTION",
                        tx_id,
                        decimal_dumps({
                            "position_value": str(position_value),
                            "correlation_id": correlation_id
                        })
                    )
                )

                await conn.execute(
                    "INSERT INTO audit_log (operation, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
                    (
                        "POST",
                        "TRANSACTION",
                        tx_id,
                        decimal_dumps({
                            "lines": 2,
                            "correlation_id": correlation_id
                        })
                    )
                )

                await conn.commit()

                # Invalidate caches
                self.equity_cache.clear()
                self.position_cache.clear()

                logger.info(
                    "trade_entry_recorded",
                    position_id=position_id,
                    market_id=market_id,
                    entry_price=str(price),
                    quantity=str(quantity),
                    cost=str(position_value)
                )

                return position_id

            except Exception as e:
                await conn.rollback()
                logger.error(
                    "trade_entry_failed",
                    error=str(e),
                    market_id=market_id
                )
                raise

            finally:
                await self.pool.release(conn)

    async def record_trade_exit(
        self,
        position_id: int,
        exit_price: Decimal,
        fees: Decimal = Decimal("0"),
        exit_reason: str = "exit",
        correlation_id: Optional[str] = None,
        exit_order_id: Optional[str] = None,
    ) -> None:
        """Close an open position and record exit in ledger."""
        if exit_price <= 0:
            raise ValueError("exit_price must be positive")

        async with self._write_lock:
            conn = await self.pool.acquire()
            try:
                await conn.execute("BEGIN TRANSACTION")

                cursor = await conn.execute(
                    """
                    SELECT market_id, token_id, strategy, side, entry_price, quantity
                    FROM positions
                    WHERE id = ? AND status = 'OPEN'
                    """,
                    (position_id,)
                )
                row = await cursor.fetchone()
                if not row:
                    raise ValueError("position_not_found")

                market_id, token_id, strategy, side, entry_price, quantity = row
                entry_price = Decimal(str(entry_price))
                quantity = Decimal(str(quantity))
                exit_price = Decimal(str(exit_price))
                fees = Decimal(str(fees))

                entry_value = entry_price * quantity
                exit_value = exit_price * quantity
                pnl = exit_value - entry_value - fees

                cursor = await conn.execute(
                    """
                    INSERT INTO transactions (description, transaction_type, strategy, reference_id, timestamp)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (f"Exit {side} position {position_id}", "TRADE_EXIT", strategy, exit_order_id or str(position_id))
                )
                tx_id = cursor.lastrowid

                cursor = await conn.execute(
                    "SELECT id FROM accounts WHERE account_name = 'Positions' LIMIT 1"
                )
                positions_account = (await cursor.fetchone())[0]

                cursor = await conn.execute(
                    "SELECT id FROM accounts WHERE account_name = 'Cash' LIMIT 1"
                )
                cash_account = (await cursor.fetchone())[0]

                cursor = await conn.execute(
                    "SELECT id FROM accounts WHERE account_name = 'Trading Revenue' LIMIT 1"
                )
                revenue_account = (await cursor.fetchone())[0]

                cursor = await conn.execute(
                    "SELECT id FROM accounts WHERE account_name = 'Trading Loss' LIMIT 1"
                )
                loss_account = (await cursor.fetchone())[0]

                await conn.execute(
                    "INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (?, ?, ?)",
                    (tx_id, cash_account, str(exit_value - fees))
                )

                await conn.execute(
                    "INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (?, ?, ?)",
                    (tx_id, positions_account, str(-entry_value))
                )

                if pnl >= 0:
                    await conn.execute(
                        "INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (?, ?, ?)",
                        (tx_id, revenue_account, str(-pnl))
                    )
                else:
                    await conn.execute(
                        "INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (?, ?, ?)",
                        (tx_id, loss_account, str(abs(pnl)))
                    )

                await conn.execute(
                    """
                    UPDATE positions
                    SET exit_price = ?, exit_timestamp = CURRENT_TIMESTAMP,
                        realized_pnl = ?, status = 'CLOSED',
                        exit_order_id = ?, exit_fees = ?
                    WHERE id = ?
                    """,
                    (str(exit_price), str(pnl), exit_order_id, str(fees), position_id)
                )

                await conn.execute(
                    """
                    INSERT INTO audit_log
                    (operation, entity_type, entity_id, old_state, new_state, reason, context, correlation_id, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "STATE_CHANGE",
                        "position",
                        str(position_id),
                        "OPEN",
                        "CLOSED",
                        exit_reason,
                        decimal_dumps({
                            "market_id": market_id,
                            "token_id": token_id,
                            "exit_price": str(exit_price),
                            "quantity": str(quantity),
                            "pnl": str(pnl),
                        }),
                        correlation_id,
                        decimal_dumps({
                            "exit_order_id": exit_order_id,
                            "fees": str(fees),
                            "exit_reason": exit_reason,
                        }),
                    )
                )

                await conn.commit()

                self.equity_cache.clear()
                self.position_cache.clear()

            except Exception:
                await conn.rollback()
                raise
            finally:
                await self.pool.release(conn)

    async def record_deposit(self, amount: Decimal, description: str = "Initial deposit") -> int:
        """
        Record a cash deposit using double-entry accounting.

        This increases the Cash account (asset).
        In double-entry accounting, we need both sides:
        - DEBIT: Cash account (asset increases)
        - CREDIT: Equity account (owner's equity increases)

        Args:
            amount: Deposit amount (Decimal)
            description: Description of the deposit

        Returns:
            transaction_id: The ID of the created transaction
        """
        amount = Decimal(str(amount))

        async with self._write_lock:
            conn = await self.pool.acquire()
            try:
                await conn.execute("BEGIN")
                cursor = await conn.execute(
                    """
                    INSERT INTO transactions (description, transaction_type, strategy, reference_id, timestamp)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (description, "DEPOSIT", "SYSTEM", "INITIAL_DEPOSIT")
                )
                txn_id = cursor.lastrowid

                cursor = await conn.execute("SELECT id FROM accounts WHERE account_name = 'Cash'")
                cash_account = (await cursor.fetchone())[0]

                cursor = await conn.execute("SELECT id FROM accounts WHERE account_name = 'Owner Equity'")
                equity_account = (await cursor.fetchone())[0]

                await conn.execute(
                    "INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (?, ?, ?)",
                    (txn_id, cash_account, str(amount))
                )

                await conn.execute(
                    "INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (?, ?, ?)",
                    (txn_id, equity_account, str(-amount))
                )

                await conn.execute(
                    "INSERT INTO audit_log (operation, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
                    (
                        "CREATE",
                        "TRANSACTION",
                        txn_id,
                        decimal_dumps({"amount": amount, "type": "DEPOSIT"})
                    )
                )

                await conn.execute("COMMIT")

                self.equity_cache.clear()
                self.position_cache.clear()

                return txn_id

            except Exception:
                await conn.execute("ROLLBACK")
                raise

            finally:
                await self.pool.release(conn)

    async def close_stale_positions(self) -> int:
        """
        Mark all currently OPEN positions as STALE_CLOSED on bot startup.

        This prevents phantom positions from previous runs from inflating
        the aggregate exposure counter and blocking new trades.

        No double-entry accounting entries are written — these positions
        are assumed to have been orphaned by a previous crash or restart.
        Their existence in the DB is preserved for audit purposes; only
        the status column changes.

        Returns:
            Number of positions marked as STALE_CLOSED.
        """
        conn = await self.pool.acquire()
        try:
            cursor = await conn.execute(
                """
                UPDATE positions
                SET status = 'CLOSED',
                    exit_timestamp = CURRENT_TIMESTAMP
                WHERE status = 'OPEN'
                """
            )
            count = cursor.rowcount if cursor.rowcount is not None else 0
            await conn.commit()
        finally:
            await self.pool.release(conn)

        logger.info("stale_positions_cleared", count=count)
        return count

    async def get_open_positions(self) -> List[PositionData]:
        """
        Get all open positions.

        Returns:
            List of PositionData objects
        """
        rows = await self._execute_query(
            """
            SELECT 
                id, market_id, token_id, strategy,
                entry_price, quantity, current_price,
                unrealized_pnl, realized_pnl, status,
                entry_timestamp, exit_timestamp,
                CAST((julianday('now') - julianday(entry_timestamp)) * 86400 AS INTEGER)
            FROM positions
            WHERE status = 'OPEN'
            ORDER BY entry_timestamp DESC
            """,
            fetch_all=True
        )

        positions = []
        for row in rows:
            positions.append(PositionData(
                id=row[0],
                market_id=row[1],
                token_id=row[2],
                strategy=row[3],
                entry_price=Decimal(str(row[4])),
                quantity=Decimal(str(row[5])),
                current_price=Decimal(str(row[6])) if row[6] is not None else None,
                unrealized_pnl=Decimal(str(row[7])),
                realized_pnl=Decimal(str(row[8])),
                status=row[9],
                entry_timestamp=datetime.fromisoformat(row[10]),
                exit_timestamp=datetime.fromisoformat(row[11]) if row[11] else None,
                hold_time_seconds=float(row[12]) if row[12] is not None else 0.0
            ))

        return positions

    async def validate_ledger(self) -> bool:
        """
        Validate ledger integrity.

        Checks:
        1. All transactions balance to zero

        Returns:
            True if valid
        """
        result = await self._execute_query(
            """
            SELECT transaction_id, SUM(amount)
            FROM transaction_lines
            GROUP BY transaction_id
            HAVING ABS(SUM(amount)) > 0.01
            """,
            fetch_all=True
        )

        if result:
            unbalanced = [(row[0], row[1]) for row in result]
            logger.error(
                "ledger_validation_failed",
                reason="unbalanced_transactions",
                transactions=unbalanced
            )
            raise AssertionError(f"Unbalanced transactions: {unbalanced}")

        logger.info("ledger_validation_passed")
        return True

    async def get_equity(self) -> Decimal:
        """
        Get current total equity (cached).
        
        Returns:
            Total equity in USD
        """
        # Check cache (guard against TTL eviction race)
        try:
            equity = self.equity_cache['equity']
            self.cache_hits += 1
            return equity
        except KeyError:
            self.cache_misses += 1

        txn_balance = await self.execute_scalar(
            "SELECT COALESCE(SUM(amount), 0) FROM transaction_lines tl "
            "JOIN accounts a ON tl.account_id=a.id WHERE a.account_type='ASSET'"
        )
        stored_balance = await self.execute_scalar(
            "SELECT COALESCE(SUM(balance), 0) FROM accounts WHERE account_type='ASSET'"
        )

        txn_equity = Decimal(str(txn_balance)) if txn_balance is not None else Decimal('0')
        stored_equity = Decimal(str(stored_balance)) if stored_balance is not None else Decimal('0')

        if abs(txn_equity - stored_equity) > Decimal('0.01'):
            logger.error(
                "equity_mismatch",
                calculated=str(txn_equity),
                stored=str(stored_equity)
            )

        equity = txn_equity

        # Update cache
        self.equity_cache['equity'] = equity

        logger.debug("equity_fetched", equity=str(equity))

        return equity

    # ------------------------------------------------------------------ order tracking

    # Lifecycle states that indicate an order is still active.
    _OPEN_ORDER_STATES = frozenset({
        "CREATED", "SUBMITTED", "PARTIALLY_FILLED", "FILLED",
    })
    _TERMINAL_ORDER_STATES = frozenset({
        "CANCELLED", "EXPIRED", "SETTLED", "ERROR",
    })

    async def record_order_created(
        self,
        *,
        order_id: str,
        market_id: str,
        token_id: str = "",
        outcome: str,
        side: str = "BUY",
        size: Decimal,
        price: Decimal,
        charlie_p_win: Optional[Decimal] = None,
        charlie_conf: Optional[Decimal] = None,
        charlie_regime: Optional[str] = None,
        strategy: Optional[str] = None,
        model_votes: Optional[Dict] = None,
        notes: Optional[str] = None,
    ) -> None:
        """
        Insert a new order row in state CREATED (idempotent on order_id).

        Called immediately before the API call so a crash after submission
        still leaves a CREATED row that reconcile_open_orders() will pick up
        and reconcile on the next startup.
        """
        import json as _json
        now = datetime.now(timezone.utc).isoformat()
        votes_json = _json.dumps(model_votes) if model_votes is not None else None
        await self.execute(
            """
            INSERT INTO order_tracking
                (order_id, market_id, token_id, outcome, side, size, price,
                 order_state, opened_at, charlie_p_win, charlie_conf,
                 charlie_regime, strategy, model_votes, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'CREATED', ?,
                    ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO NOTHING
            """,
            (
                order_id,
                market_id,
                token_id,
                outcome.upper(),
                side.upper(),
                str(size),
                str(price),
                now,
                str(charlie_p_win) if charlie_p_win is not None else None,
                str(charlie_conf) if charlie_conf is not None else None,
                charlie_regime,
                strategy,
                votes_json,
                notes,
            ),
        )

    async def transition_order_state(
        self,
        order_id: str,
        new_state: str,
        *,
        pnl: Optional[Decimal] = None,
        notes: Optional[str] = None,
    ) -> None:
        """
        Fast-path state update — only changes order_state, pnl, closed_at,
        and notes.  Does not touch any other column.
        """
        closed_at = (
            datetime.now(timezone.utc).isoformat()
            if new_state.upper() in self._TERMINAL_ORDER_STATES
            else None
        )
        await self.execute(
            """
            UPDATE order_tracking
            SET order_state = ?,
                pnl         = COALESCE(?, pnl),
                closed_at   = COALESCE(?, closed_at),
                notes       = COALESCE(?, notes)
            WHERE order_id = ?
            """,
            (
                new_state.upper(),
                str(pnl) if pnl is not None else None,
                closed_at,
                notes,
                order_id,
            ),
        )

    async def get_open_orders(self) -> List[Dict]:
        """Return all order_tracking rows in OPEN_ORDER_STATES as plain dicts."""
        placeholders = ",".join("?" * len(self._OPEN_ORDER_STATES))
        states = list(self._OPEN_ORDER_STATES)
        rows = await self._execute_query(
            f"SELECT * FROM order_tracking WHERE order_state IN ({placeholders})"
            " ORDER BY opened_at ASC",
            params=tuple(states),
            fetch_all=True,
        )
        return [dict(zip([d[0] for d in row.description], row)) if hasattr(row, 'description') else dict(row) for row in (rows or [])]

    async def _order_rows_as_dicts(self, rows) -> List[Dict]:
        """Convert aiosqlite Row objects to plain dicts."""
        if not rows:
            return []
        result = []
        for row in rows:
            # aiosqlite Row supports keys() and index access
            try:
                result.append(dict(row))
            except TypeError:
                # Fallback: row is a plain tuple — fetch column names via description
                result.append({f"col_{i}": v for i, v in enumerate(row)})
        return result

    async def get_all_tracked_orders(self, limit: int = 500) -> List[Dict]:
        """Return the most recent `limit` order tracking rows."""
        rows = await self._execute_query(
            "SELECT * FROM order_tracking ORDER BY opened_at DESC LIMIT ?",
            params=(limit,),
            fetch_all=True,
        )
        return await self._order_rows_as_dicts(rows)

    async def reconcile_open_orders(self, api_client) -> Dict:
        """
        On startup: compare order_tracking open rows against current exchange
        state and close out any that resolved while the process was offline.

        Returns a summary dict that ``main.py`` logs on startup:
          open_orders              – total rows queried
          resolved_while_offline   – rows transitioned to SETTLED/CANCELLED/EXPIRED
          still_open               – rows still in an open state
          recovered_pnl            – sum of PnL for settled rows
        """
        placeholders = ",".join("?" * len(self._OPEN_ORDER_STATES))
        states = list(self._OPEN_ORDER_STATES)
        rows = await self._execute_query(
            f"SELECT * FROM order_tracking WHERE order_state IN ({placeholders})"
            " ORDER BY opened_at ASC",
            params=tuple(states),
            fetch_all=True,
        )
        open_orders = await self._order_rows_as_dicts(rows)

        if not open_orders:
            logger.info("ledger_reconcile_no_open_orders")
            return {
                "open_orders": 0,
                "resolved_while_offline": 0,
                "still_open": 0,
                "recovered_pnl": Decimal("0"),
            }

        logger.info("ledger_reconcile_start", open_order_count=len(open_orders))
        resolved_count = 0
        still_open_count = 0
        recovered_pnl = Decimal("0")

        for row in open_orders:
            order_id = row.get("order_id", "")
            market_id = row.get("market_id", "")
            size_str = row.get("size", "0")
            price_str = row.get("price", "0")
            size = Decimal(str(size_str))
            price = Decimal(str(price_str))

            exchange_state: Optional[str] = None
            market_resolved = False
            winning_side: Optional[str] = None
            payout_per_share: Optional[Decimal] = None

            try:
                if hasattr(api_client, "get_order_status"):
                    status = await asyncio.wait_for(
                        api_client.get_order_status(order_id), timeout=10.0
                    )
                    if status:
                        exchange_state = (status.get("status") or "").upper()
            except (asyncio.TimeoutError, Exception) as exc:
                logger.warning("ledger_reconcile_order_status_error",
                               order_id=order_id, error=str(exc))

            try:
                if hasattr(api_client, "get_market"):
                    market = await asyncio.wait_for(
                        api_client.get_market(market_id), timeout=10.0
                    )
                    if market:
                        market_resolved = bool(
                            market.get("closed") or market.get("resolved")
                        )
                        winning_side = market.get("winning_side") or market.get("outcome")
                        raw_payout = market.get("payout_numerator") or market.get(
                            "payout_per_share"
                        )
                        if raw_payout is not None:
                            payout_per_share = Decimal(str(raw_payout))
            except (asyncio.TimeoutError, Exception) as exc:
                logger.warning("ledger_reconcile_market_status_error",
                               market_id=market_id, error=str(exc))

            if market_resolved and payout_per_share is not None:
                quantity = size / price if price > Decimal("0") else Decimal("0")
                pnl = quantity * payout_per_share - size
                await self.transition_order_state(
                    order_id, "SETTLED", pnl=pnl,
                    notes=f"resolved_offline winning_side={winning_side} payout={payout_per_share}",
                )
                recovered_pnl += pnl
                resolved_count += 1
                logger.info("ledger_order_settled_offline",
                            order_id=order_id, market_id=market_id,
                            pnl=str(pnl), winning_side=winning_side)

            elif exchange_state in {"CANCELLED", "EXPIRED"}:
                new_state = "CANCELLED" if exchange_state == "CANCELLED" else "EXPIRED"
                await self.transition_order_state(
                    order_id, new_state, notes="resolved_offline"
                )
                resolved_count += 1
                logger.info("ledger_order_closed_offline",
                            order_id=order_id, exchange_state=exchange_state)
            else:
                still_open_count += 1

        summary = {
            "open_orders": len(open_orders),
            "resolved_while_offline": resolved_count,
            "still_open": still_open_count,
            "recovered_pnl": recovered_pnl,
        }
        logger.info("ledger_reconcile_complete",
                    **{k: str(v) for k, v in summary.items()})
        return summary

    async def shutdown_snapshot(self, price_feed=None) -> Dict:
        """
        Log a final human-readable snapshot of open order exposure on shutdown.
        Called by ``main.py`` inside ``stop()`` before closing the ledger.
        """
        import json as _json
        rows = await self._execute_query(
            "SELECT * FROM order_tracking WHERE order_state IN "
            "('CREATED','SUBMITTED','PARTIALLY_FILLED','FILLED')"
            " ORDER BY opened_at ASC",
            fetch_all=True,
        )
        open_orders = await self._order_rows_as_dicts(rows)

        total_exposure = Decimal("0")
        mark_pnl = Decimal("0")

        for row in open_orders:
            size = Decimal(str(row.get("size", "0")))
            total_exposure += size
            if price_feed is not None:
                try:
                    mid = await asyncio.wait_for(
                        price_feed.get_price(row["market_id"]), timeout=3.0
                    )
                    if mid is not None:
                        mark_price = Decimal(str(mid))
                        entry_price = Decimal(str(row.get("price", "0")))
                        quantity = size / entry_price if entry_price > Decimal("0") else Decimal("0")
                        mark_pnl += (mark_price - entry_price) * quantity
                except Exception:
                    pass

        # Realized PnL from settled orders
        realized_pnl_row = await self.execute_scalar(
            "SELECT SUM(CAST(pnl AS REAL)) FROM order_tracking"
            " WHERE order_state = 'SETTLED' AND pnl IS NOT NULL"
        )
        realized_pnl = (
            Decimal(str(realized_pnl_row)) if realized_pnl_row is not None else Decimal("0")
        )

        settled_count = await self.execute_scalar(
            "SELECT COUNT(*) FROM order_tracking WHERE order_state = 'SETTLED'"
        ) or 0
        win_count = await self.execute_scalar(
            "SELECT COUNT(*) FROM order_tracking"
            " WHERE order_state = 'SETTLED' AND CAST(pnl AS REAL) > 0"
        ) or 0
        hit_rate = (win_count / settled_count) if settled_count > 0 else None

        snapshot = {
            "open_positions": len(open_orders),
            "total_exposure_usdc": str(total_exposure),
            "mark_to_market_pnl": str(mark_pnl),
            "realized_pnl_all_time": str(realized_pnl),
            "hit_rate": hit_rate,
            "markets": list({row["market_id"] for row in open_orders}),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("ledger_shutdown_snapshot",
                    **{k: str(v) for k, v in snapshot.items()})
        return snapshot

    async def get_positions_by_market(self) -> List[Dict]:
        """
        Return aggregate exposure for every market that has at least one OPEN
        position.

        Groups by ``market_id`` so callers get a single row per market rather
        than one row per position.  Useful for portfolio-level risk checks.

        Returns a list of dicts with keys:
          market_id      – Polymarket condition_id / market slug
          token_id       – token ID of the first open leg (most markets have
                           one open leg at a time)
          open_lots      – number of open sub-positions in this market
          total_quantity – aggregate quantity across all open legs (Decimal)
          avg_entry_price– quantity-weighted average entry price (Decimal)
          strategy       – strategy tag from the first open position
        """
        rows = await self._execute_query(
            """
            SELECT
                market_id,
                MAX(token_id)                                          AS token_id,
                COUNT(*)                                               AS open_lots,
                SUM(CAST(quantity AS REAL))                            AS total_quantity,
                SUM(CAST(quantity AS REAL) * CAST(entry_price AS REAL))
                    / NULLIF(SUM(CAST(quantity AS REAL)), 0)           AS avg_entry_price,
                MAX(strategy)                                          AS strategy
            FROM positions
            WHERE status = 'OPEN'
            GROUP BY market_id
            ORDER BY total_quantity DESC
            """,
            fetch_all=True,
        )
        result = []
        for row in (rows or []):
            result.append(
                {
                    "market_id":        row[0],
                    "token_id":         row[1],
                    "open_lots":        int(row[2]),
                    "total_quantity":   Decimal(str(row[3])) if row[3] is not None else Decimal("0"),
                    "avg_entry_price":  Decimal(str(row[4])) if row[4] is not None else Decimal("0"),
                    "strategy":         row[5] or "",
                }
            )
        return result

    async def get_metrics(self) -> Dict:
        """
        Get ledger metrics.

        Returns:
            Metrics dictionary
        """
        avg_query_time = (
            self.total_query_time_ms / self.queries_executed
            if self.queries_executed > 0 else 0.0
        )

        cache_hit_rate = (
            self.cache_hits / (self.cache_hits + self.cache_misses)
            if (self.cache_hits + self.cache_misses) > 0 else 0.0
        )

        return {
            "queries_executed": self.queries_executed,
            "avg_query_time_ms": avg_query_time,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": cache_hit_rate,
            "db_path": self.db_path
        }

    async def close(self):
        """Close ledger and cleanup resources."""
        await self.pool.close_all()
        
        logger.info(
            "async_ledger_closed",
            queries_executed=self.queries_executed,
            cache_hit_rate=self.cache_hits / max(1, self.cache_hits + self.cache_misses)
        )

    @asynccontextmanager
    async def transaction(self):
        conn = await self.pool.acquire()
        try:
            await conn.execute("BEGIN TRANSACTION")
            yield _LedgerTransaction(conn)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await self.pool.release(conn)


class _LedgerTransaction:
    """Scoped transaction helper for AsyncLedger."""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def execute(self, query: str, params: Tuple = ()) -> aiosqlite.Cursor:
        return await self._conn.execute(query, params)

    async def execute_scalar(self, query: str, params: Tuple = ()) -> Any:
        cursor = await self._conn.execute(query, params)
        row = await cursor.fetchone()
        return row[0] if row else None

    async def last_insert_row_id(self) -> int:
        cursor = await self._conn.execute("SELECT last_insert_rowid()")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
