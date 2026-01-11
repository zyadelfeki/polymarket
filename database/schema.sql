-- Production-Grade Trading Bot Database Schema
-- Double-Entry Accounting System for Polymarket Bot

-- Core Accounts Table
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_type TEXT NOT NULL CHECK(account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')),
    name TEXT NOT NULL UNIQUE,
    currency TEXT NOT NULL DEFAULT 'USDC',
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
);

-- Insert standard accounts
INSERT OR IGNORE INTO accounts (account_type, name, description) VALUES
    ('ASSET', 'cash', 'Main trading capital (USDC)'),
    ('ASSET', 'positions_open', 'Value of open positions at entry price'),
    ('ASSET', 'positions_unrealized', 'Unrealized PnL on open positions'),
    ('EXPENSE', 'trading_fees', 'Polymarket trading fees'),
    ('EXPENSE', 'slippage', 'Price slippage on fills'),
    ('REVENUE', 'trading_profit', 'Realized trading profits'),
    ('EQUITY', 'retained_earnings', 'Cumulative net profit/loss');

-- Transactions Table (header)
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_type TEXT NOT NULL CHECK(transaction_type IN (
        'DEPOSIT', 'WITHDRAWAL', 'TRADE_ENTRY', 'TRADE_EXIT', 'FEE', 'ADJUSTMENT'
    )),
    strategy TEXT,  -- 'latency_arb', 'whale_copy', 'liquidity_shock', 'ml_ensemble'
    reference_id TEXT,  -- External ID (order ID, trade ID, etc.)
    description TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT  -- JSON for additional data
);

-- Transaction Lines Table (double-entry)
-- Every transaction MUST have lines that sum to zero
CREATE TABLE IF NOT EXISTS transaction_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    amount DECIMAL(20, 8) NOT NULL,  -- Positive = debit, Negative = credit
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (transaction_id) REFERENCES transactions(id),
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    CHECK (amount != 0)  -- No zero-value lines
);

-- Index for performance
CREATE INDEX IF NOT EXISTS idx_transaction_lines_txn ON transaction_lines(transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_lines_account ON transaction_lines(account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_timestamp ON transactions(timestamp);
CREATE INDEX IF NOT EXISTS idx_transactions_strategy ON transactions(strategy);

-- Positions Table (live tracking)
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,  -- Polymarket condition_id
    token_id TEXT NOT NULL,   -- Specific token (YES or NO)
    side TEXT NOT NULL CHECK(side IN ('YES', 'NO')),
    quantity DECIMAL(20, 8) NOT NULL,
    entry_price DECIMAL(10, 6) NOT NULL,
    current_price DECIMAL(10, 6),  -- Updated via polling
    entry_transaction_id INTEGER,
    strategy TEXT NOT NULL,
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    status TEXT DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'CLOSED')),
    metadata TEXT,  -- JSON: question, expected_edge, etc.
    FOREIGN KEY (entry_transaction_id) REFERENCES transactions(id)
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy);

-- Orders Table (audit trail)
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT UNIQUE NOT NULL,  -- Polymarket order ID
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    order_type TEXT NOT NULL CHECK(order_type IN ('GTC', 'FOK', 'GTD')),
    quantity DECIMAL(20, 8) NOT NULL,
    price DECIMAL(10, 6) NOT NULL,
    filled_quantity DECIMAL(20, 8) DEFAULT 0,
    status TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'OPEN', 'MATCHED', 'CANCELLED', 'FAILED')),
    strategy TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    filled_at TIMESTAMP,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);

-- Price History (for backtesting & analysis)
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,  -- 'BTC', 'ETH', 'SOL'
    source TEXT NOT NULL,  -- 'BINANCE', 'COINBASE'
    price DECIMAL(20, 8) NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_price_history_symbol_ts ON price_history(symbol, timestamp DESC);

-- Market Snapshots (Polymarket orderbook depth)
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    yes_price DECIMAL(10, 6),
    no_price DECIMAL(10, 6),
    yes_liquidity DECIMAL(20, 2),
    no_liquidity DECIMAL(20, 2),
    orderbook_depth TEXT,  -- JSON of full orderbook
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_ts ON market_snapshots(market_id, timestamp DESC);

-- Circuit Breaker Events
CREATE TABLE IF NOT EXISTS circuit_breaker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK(event_type IN ('TRIGGERED', 'RESET', 'MANUAL_OVERRIDE')),
    reason TEXT,
    drawdown_pct DECIMAL(10, 4),
    equity_at_trigger DECIMAL(20, 8),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Health Check Log
CREATE TABLE IF NOT EXISTS health_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,  -- 'BINANCE_WS', 'POLYMARKET_API', 'DB', 'STRATEGY_X'
    status TEXT NOT NULL CHECK(status IN ('HEALTHY', 'DEGRADED', 'FAILED')),
    latency_ms INTEGER,
    error_message TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_health_checks_component_ts ON health_checks(component, timestamp DESC);

-- Strategy Performance Metrics
CREATE TABLE IF NOT EXISTS strategy_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    metric_date DATE NOT NULL,
    trades_count INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_pnl DECIMAL(20, 8) DEFAULT 0,
    max_drawdown_pct DECIMAL(10, 4),
    sharpe_ratio DECIMAL(10, 4),
    avg_hold_time_seconds INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(strategy, metric_date)
);

-- Views for easy querying

-- Current Equity (Asset accounts - Liability accounts)
CREATE VIEW IF NOT EXISTS v_current_equity AS
SELECT 
    SUM(CASE WHEN a.account_type = 'ASSET' THEN tl.amount ELSE 0 END) -
    SUM(CASE WHEN a.account_type = 'LIABILITY' THEN tl.amount ELSE 0 END) as equity,
    MAX(tl.created_at) as as_of_timestamp
FROM transaction_lines tl
JOIN accounts a ON tl.account_id = a.id;

-- Strategy PnL Summary
CREATE VIEW IF NOT EXISTS v_strategy_pnl AS
SELECT 
    t.strategy,
    COUNT(DISTINCT t.id) as trade_count,
    SUM(CASE WHEN tl.amount > 0 THEN tl.amount ELSE 0 END) as total_profit,
    SUM(CASE WHEN tl.amount < 0 THEN ABS(tl.amount) ELSE 0 END) as total_loss,
    SUM(tl.amount) as net_pnl,
    MIN(t.timestamp) as first_trade,
    MAX(t.timestamp) as last_trade
FROM transactions t
JOIN transaction_lines tl ON t.id = tl.transaction_id
JOIN accounts a ON tl.account_id = a.id
WHERE a.name = 'trading_profit' AND t.strategy IS NOT NULL
GROUP BY t.strategy;

-- Open Positions Summary
CREATE VIEW IF NOT EXISTS v_open_positions AS
SELECT 
    p.id,
    p.market_id,
    p.strategy,
    p.side,
    p.quantity,
    p.entry_price,
    p.current_price,
    (p.current_price - p.entry_price) * p.quantity as unrealized_pnl,
    ((p.current_price - p.entry_price) / p.entry_price) as unrealized_roi,
    (julianday('now') - julianday(p.opened_at)) * 86400 as hold_time_seconds
FROM positions p
WHERE p.status = 'OPEN';

-- Trigger to enforce double-entry invariant
CREATE TRIGGER IF NOT EXISTS trg_check_balanced_transaction
AFTER INSERT ON transaction_lines
BEGIN
    SELECT CASE
        WHEN (
            SELECT ABS(SUM(amount)) 
            FROM transaction_lines 
            WHERE transaction_id = NEW.transaction_id
        ) > 0.01  -- Allow rounding error up to 0.01
        THEN RAISE(ABORT, 'Transaction lines must sum to zero')
    END;
END;

-- Validation function (run periodically)
-- SELECT transaction_id, SUM(amount) as balance 
-- FROM transaction_lines 
-- GROUP BY transaction_id 
-- HAVING ABS(balance) > 0.01;