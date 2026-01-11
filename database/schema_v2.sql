-- ==================================================================
-- INSTITUTIONAL-GRADE DATABASE SCHEMA
-- ==================================================================
--
-- Features:
-- - Optimized indexes for all hot queries
-- - Proper foreign key constraints
-- - Audit columns (created_at, updated_at)
-- - Efficient data types
-- - Query performance targets: <10ms
--
-- Standards:
-- - All IDs are INTEGER PRIMARY KEY (auto-increment)
-- - All timestamps are TEXT in ISO 8601 format
-- - All decimal values use REAL (sufficient precision for money)
-- - Proper indexing on all foreign keys
-- - Composite indexes for common query patterns
--
-- ==================================================================

-- Enable WAL mode for better concurrency
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=10000;
PRAGMA temp_store=MEMORY;

-- ==================================================================
-- ACCOUNTS TABLE
-- Double-entry bookkeeping accounts
-- ==================================================================

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL UNIQUE,
    account_type TEXT NOT NULL CHECK(account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')),
    balance REAL DEFAULT 0.0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Index for type filtering
CREATE INDEX IF NOT EXISTS idx_accounts_type ON accounts(account_type);

-- Index for name lookups
CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts(account_name);

-- ==================================================================
-- TRANSACTIONS TABLE
-- Double-entry accounting transactions
-- ==================================================================

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    description TEXT,
    transaction_type TEXT CHECK(transaction_type IN ('DEPOSIT', 'WITHDRAWAL', 'TRADE_ENTRY', 'TRADE_EXIT', 'FEE', 'ADJUSTMENT')),
    reference_id TEXT,  -- External reference (order_id, position_id, etc.)
    created_at TEXT DEFAULT (datetime('now'))
);

-- Index for timestamp queries (most common)
CREATE INDEX IF NOT EXISTS idx_transactions_timestamp ON transactions(timestamp DESC);

-- Index for type filtering
CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions(transaction_type);

-- Index for reference lookups
CREATE INDEX IF NOT EXISTS idx_transactions_reference ON transactions(reference_id);

-- Composite index for date range + type queries
CREATE INDEX IF NOT EXISTS idx_transactions_timestamp_type ON transactions(timestamp DESC, transaction_type);

-- ==================================================================
-- TRANSACTION LINES TABLE
-- Individual debit/credit entries
-- ==================================================================

CREATE TABLE IF NOT EXISTS transaction_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    amount REAL NOT NULL,  -- Positive = debit, Negative = credit
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE RESTRICT
);

-- Index for transaction lookup (most common)
CREATE INDEX IF NOT EXISTS idx_tlines_transaction ON transaction_lines(transaction_id);

-- Index for account balance calculation (hot query)
CREATE INDEX IF NOT EXISTS idx_tlines_account ON transaction_lines(account_id);

-- Composite index for account + transaction lookup
CREATE INDEX IF NOT EXISTS idx_tlines_account_transaction ON transaction_lines(account_id, transaction_id);

-- ==================================================================
-- POSITIONS TABLE
-- Trading positions tracking
-- ==================================================================

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    
    -- Entry details
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    entry_timestamp TEXT NOT NULL,
    entry_transaction_id INTEGER,
    order_id TEXT,
    
    -- Current state
    current_price REAL,
    last_price_update TEXT,
    
    -- P&L tracking
    unrealized_pnl REAL DEFAULT 0.0,
    realized_pnl REAL DEFAULT 0.0,
    fees_paid REAL DEFAULT 0.0,
    
    -- Status
    status TEXT DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'CLOSED', 'PARTIALLY_CLOSED')),
    
    -- Exit details
    exit_price REAL,
    exit_timestamp TEXT,
    exit_transaction_id INTEGER,
    
    -- Metadata
    metadata TEXT,  -- JSON for additional data
    
    -- Timestamps
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    
    FOREIGN KEY(entry_transaction_id) REFERENCES transactions(id),
    FOREIGN KEY(exit_transaction_id) REFERENCES transactions(id)
);

-- Index for open positions query (VERY HOT)
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

-- Index for market lookup
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);

-- Index for strategy analysis
CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy);

-- Composite index for open positions by strategy (common query)
CREATE INDEX IF NOT EXISTS idx_positions_status_strategy ON positions(status, strategy);

-- Composite index for market + status (order book analysis)
CREATE INDEX IF NOT EXISTS idx_positions_market_status ON positions(market_id, status);

-- Index for entry timestamp (for aging analysis)
CREATE INDEX IF NOT EXISTS idx_positions_entry_time ON positions(entry_timestamp DESC);

-- Index for order ID lookups
CREATE INDEX IF NOT EXISTS idx_positions_order ON positions(order_id);

-- ==================================================================
-- ORDERS TABLE
-- Order tracking (matches, partial fills, etc.)
-- ==================================================================

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL UNIQUE,  -- External order ID
    
    -- Order details
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL', 'YES', 'NO')),
    order_type TEXT NOT NULL CHECK(order_type IN ('MARKET', 'LIMIT', 'FOK', 'GTT')),
    
    -- Quantity
    quantity REAL NOT NULL,
    filled_quantity REAL DEFAULT 0.0,
    remaining_quantity REAL,
    
    -- Pricing
    limit_price REAL,
    average_fill_price REAL,
    
    -- Status
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED')),
    
    -- Strategy context
    strategy TEXT NOT NULL,
    position_id INTEGER,  -- Links to position if applicable
    
    -- Fees
    fees_paid REAL DEFAULT 0.0,
    
    -- Timestamps
    created_at TEXT DEFAULT (datetime('now')),
    submitted_at TEXT,
    filled_at TEXT,
    updated_at TEXT DEFAULT (datetime('now')),
    
    -- Metadata
    metadata TEXT,  -- JSON
    
    FOREIGN KEY(position_id) REFERENCES positions(id)
);

-- Index for order ID lookup (VERY HOT)
CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id);

-- Index for status filtering (monitor pending orders)
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Composite index for active orders by strategy
CREATE INDEX IF NOT EXISTS idx_orders_status_strategy ON orders(status, strategy);

-- Index for market analysis
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);

-- Index for position linking
CREATE INDEX IF NOT EXISTS idx_orders_position ON orders(position_id);

-- Index for timestamp queries
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);

-- ==================================================================
-- ORDER FILLS TABLE
-- Individual fill tracking (for partial fills)
-- ==================================================================

CREATE TABLE IF NOT EXISTS order_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,  -- External order ID
    fill_id TEXT NOT NULL UNIQUE,  -- External fill ID
    
    -- Fill details
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL DEFAULT 0.0,
    
    -- Timestamps
    filled_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    
    -- Metadata
    metadata TEXT,
    
    FOREIGN KEY(order_id) REFERENCES orders(order_id)
);

-- Index for order lookup (common query)
CREATE INDEX IF NOT EXISTS idx_fills_order ON order_fills(order_id);

-- Index for fill ID
CREATE INDEX IF NOT EXISTS idx_fills_fill_id ON order_fills(fill_id);

-- Index for timestamp analysis
CREATE INDEX IF NOT EXISTS idx_fills_time ON order_fills(filled_at DESC);

-- ==================================================================
-- MARKET_DATA TABLE
-- Historical market data caching
-- ==================================================================

CREATE TABLE IF NOT EXISTS market_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    
    -- Pricing
    bid_price REAL,
    ask_price REAL,
    mid_price REAL,
    last_price REAL,
    
    -- Volume
    volume_24h REAL DEFAULT 0.0,
    
    -- Liquidity
    bid_liquidity REAL DEFAULT 0.0,
    ask_liquidity REAL DEFAULT 0.0,
    
    -- Timestamp
    timestamp TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Index for market lookup
CREATE INDEX IF NOT EXISTS idx_marketdata_market ON market_data(market_id);

-- Composite index for token + timestamp (common query)
CREATE INDEX IF NOT EXISTS idx_marketdata_token_time ON market_data(token_id, timestamp DESC);

-- Composite index for market + timestamp
CREATE INDEX IF NOT EXISTS idx_marketdata_market_time ON market_data(market_id, timestamp DESC);

-- ==================================================================
-- PRICE_HISTORY TABLE
-- Price snapshots for analysis
-- ==================================================================

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,  -- BTC, ETH, etc.
    source TEXT NOT NULL,  -- 'binance', 'polymarket', etc.
    
    price REAL NOT NULL,
    volume REAL DEFAULT 0.0,
    
    timestamp TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Composite index for symbol + source + time (hot query)
CREATE INDEX IF NOT EXISTS idx_pricehistory_symbol_time ON price_history(symbol, source, timestamp DESC);

-- Index for timestamp-based cleanup
CREATE INDEX IF NOT EXISTS idx_pricehistory_timestamp ON price_history(timestamp DESC);

-- ==================================================================
-- STRATEGY_METRICS TABLE
-- Per-strategy performance tracking
-- ==================================================================

CREATE TABLE IF NOT EXISTS strategy_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    
    -- Performance
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    
    total_pnl REAL DEFAULT 0.0,
    total_fees REAL DEFAULT 0.0,
    
    -- Latest trade
    last_trade_at TEXT,
    
    -- Snapshot
    snapshot_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Index for strategy lookup
CREATE INDEX IF NOT EXISTS idx_stratmetrics_strategy ON strategy_metrics(strategy);

-- Index for time-series analysis
CREATE INDEX IF NOT EXISTS idx_stratmetrics_time ON strategy_metrics(snapshot_at DESC);

-- ==================================================================
-- SYSTEM_METRICS TABLE
-- System-wide metrics snapshots
-- ==================================================================

CREATE TABLE IF NOT EXISTS system_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- Account metrics
    total_equity REAL NOT NULL,
    peak_equity REAL NOT NULL,
    drawdown_pct REAL DEFAULT 0.0,
    
    -- Position metrics
    open_positions INTEGER DEFAULT 0,
    total_exposure REAL DEFAULT 0.0,
    
    -- Order metrics
    pending_orders INTEGER DEFAULT 0,
    
    -- Performance
    daily_pnl REAL DEFAULT 0.0,
    total_pnl REAL DEFAULT 0.0,
    
    -- Snapshot
    snapshot_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Index for time-series queries
CREATE INDEX IF NOT EXISTS idx_sysmetrics_time ON system_metrics(snapshot_at DESC);

-- ==================================================================
-- ALERTS TABLE
-- Alert history
-- ==================================================================

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    alert_type TEXT NOT NULL,  -- 'CIRCUIT_BREAKER', 'HEALTH_MONITOR', etc.
    severity TEXT NOT NULL CHECK(severity IN ('INFO', 'WARNING', 'CRITICAL')),
    component TEXT,  -- Which component triggered
    
    message TEXT NOT NULL,
    metadata TEXT,  -- JSON
    
    acknowledged BOOLEAN DEFAULT 0,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    
    created_at TEXT DEFAULT (datetime('now'))
);

-- Index for unacknowledged alerts
CREATE INDEX IF NOT EXISTS idx_alerts_ack ON alerts(acknowledged, severity);

-- Index for component filtering
CREATE INDEX IF NOT EXISTS idx_alerts_component ON alerts(component);

-- Index for time-based queries
CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(created_at DESC);

-- ==================================================================
-- VIEWS (for common queries)
-- ==================================================================

-- View: Open positions with current P&L
CREATE VIEW IF NOT EXISTS v_open_positions AS
SELECT 
    p.*,
    (p.current_price - p.entry_price) * p.quantity AS unrealized_pnl_calc,
    julianday('now') - julianday(p.entry_timestamp) AS days_held
FROM positions p
WHERE p.status = 'OPEN';

-- View: Active orders
CREATE VIEW IF NOT EXISTS v_active_orders AS
SELECT o.*
FROM orders o
WHERE o.status IN ('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED');

-- View: Today's trades
CREATE VIEW IF NOT EXISTS v_todays_trades AS
SELECT p.*
FROM positions p
WHERE DATE(p.entry_timestamp) = DATE('now');

-- View: Strategy performance summary
CREATE VIEW IF NOT EXISTS v_strategy_performance AS
SELECT 
    strategy,
    COUNT(*) as total_trades,
    SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
    SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
    SUM(realized_pnl) as total_pnl,
    AVG(realized_pnl) as avg_pnl,
    SUM(fees_paid) as total_fees
FROM positions
WHERE status = 'CLOSED'
GROUP BY strategy;

-- ==================================================================
-- TRIGGERS (for data consistency)
-- ==================================================================

-- Trigger: Update accounts.updated_at on balance change
CREATE TRIGGER IF NOT EXISTS trg_accounts_update
AFTER UPDATE OF balance ON accounts
FOR EACH ROW
BEGIN
    UPDATE accounts SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- Trigger: Update positions.updated_at on any change
CREATE TRIGGER IF NOT EXISTS trg_positions_update
AFTER UPDATE ON positions
FOR EACH ROW
BEGIN
    UPDATE positions SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- Trigger: Update orders.updated_at on any change
CREATE TRIGGER IF NOT EXISTS trg_orders_update
AFTER UPDATE ON orders
FOR EACH ROW
BEGIN
    UPDATE orders SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- Trigger: Calculate remaining_quantity on order update
CREATE TRIGGER IF NOT EXISTS trg_orders_remaining
AFTER UPDATE OF filled_quantity ON orders
FOR EACH ROW
BEGIN
    UPDATE orders 
    SET remaining_quantity = quantity - filled_quantity 
    WHERE id = NEW.id;
END;

-- ==================================================================
-- INITIAL DATA
-- ==================================================================

-- Insert base accounts (if not exists)
INSERT OR IGNORE INTO accounts (account_name, account_type) VALUES
    ('Cash', 'ASSET'),
    ('Positions', 'ASSET'),
    ('Owner Equity', 'EQUITY'),
    ('Trading Revenue', 'REVENUE'),
    ('Trading Fees', 'EXPENSE'),
    ('Slippage Costs', 'EXPENSE'),
    ('Unrealized P&L', 'EQUITY');

-- ==================================================================
-- MAINTENANCE QUERIES
-- ==================================================================

-- Query: Vacuum database (compact, reclaim space)
-- Run periodically: VACUUM;

-- Query: Analyze tables (update query planner statistics)
-- Run after bulk inserts: ANALYZE;

-- Query: Check integrity
-- Run periodically: PRAGMA integrity_check;

-- Query: Database stats
-- SELECT * FROM pragma_database_list;
-- SELECT * FROM pragma_table_info('positions');

-- ==================================================================
-- QUERY PERFORMANCE TARGETS
-- ==================================================================
--
-- All queries should complete in <10ms on typical hardware:
--
-- - Get equity: <1ms (cached)
-- - Get open positions: <2ms (indexed on status)
-- - Get active orders: <2ms (indexed on status)
-- - Insert position: <5ms (with triggers)
-- - Update order: <5ms (with triggers)
-- - Query by market_id: <3ms (indexed)
-- - Query by timestamp: <3ms (indexed)
--
-- ==================================================================
