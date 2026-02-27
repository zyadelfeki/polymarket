-- Polymarket Trading System Database Schema
-- Double-entry accounting with position tracking

-- Chart of Accounts
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL UNIQUE,
    account_type TEXT NOT NULL CHECK(account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')),
    balance DECIMAL(20, 8) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Initialize standard accounts
INSERT OR IGNORE INTO accounts (account_name, account_type, balance) VALUES
    ('Cash', 'ASSET', 0),
    ('Positions', 'ASSET', 0),
    ('Unrealized PnL', 'ASSET', 0),
    ('Trading Fees', 'EXPENSE', 0),
    ('Owner Equity', 'EQUITY', 0),
    ('Trading Revenue', 'REVENUE', 0),
    ('Trading Loss', 'EXPENSE', 0);

-- Transactions (journal entries)
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

-- Transaction lines (debits and credits)
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

-- Positions (open and closed)
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER UNIQUE,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT,
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
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    entry_order_id TEXT,
    exit_order_id TEXT,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy);
CREATE INDEX IF NOT EXISTS idx_positions_entry_time ON positions(entry_timestamp);

CREATE TRIGGER IF NOT EXISTS trg_positions_set_position_id
AFTER INSERT ON positions
WHEN NEW.position_id IS NULL
BEGIN
    UPDATE positions SET position_id = NEW.id WHERE id = NEW.id;
END;

-- Triggers to maintain account balances automatically
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

-- Audit log for all operations
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

-- Idempotency log for order deduplication
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

-- Order lifecycle tracking (replaces state/orders_ledger.db)
-- This table tracks the lifecycle state of every order placed by the bot.
-- It is separate from the `positions` table (which is an accounting record of
-- fills) because an order may go through CREATED → SUBMITTED → FILLED before
-- a position row is written.  Keeping lifecycle state here avoids mutating the
-- double-entry `positions` ledger for non-accounting events.
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

CREATE INDEX IF NOT EXISTS idx_order_tracking_state    ON order_tracking(order_state);
CREATE INDEX IF NOT EXISTS idx_order_tracking_market   ON order_tracking(market_id);
CREATE INDEX IF NOT EXISTS idx_order_tracking_opened   ON order_tracking(opened_at);
-- Composite index for _get_rolling_features() hot-path query:
-- WHERE order_state='SETTLED' AND pnl IS NOT NULL AND closed_at IS NOT NULL ORDER BY closed_at DESC
CREATE INDEX IF NOT EXISTS idx_ot_settled_closed ON order_tracking(order_state, closed_at DESC);
