# 🤖 AGENT PROMPT: A-TIER CODE HARDENING

This document contains **exact, copy-paste-ready prompts** for fixing each critical issue. Use these with Copilot/Claude to implement fixes.

---

## PROMPT 1: DECIMAL PRECISION HARDENING

```
You are a financial trading bot code auditor. Your task is to implement 
DECIMAL PRECISION enforcement across the entire trading system.

CURRENT STATE (BROKEN):
- Floats used in place_order() parameters (price, size)
- Database stores REAL (float64) instead of DECIMAL
- No quantization (rounding inconsistent)
- Equity shows 10000.000000000116 instead of 10000.00

REQUIRED FIX:
1. Update polymarket_client_v2.py::place_order() signature:
   - Accept price: float, size: float (API boundary stays float)
   - IMMEDIATELY convert to Decimal(str(price)) and Decimal(str(size))
   - Apply quantize() with ROUND_HALF_UP to 4 decimal places for price, 2 for quantity
   - Validate ranges AFTER conversion (price [0.01, 0.99], quantity > 0)
   - Use Decimal values EXCLUSIVELY for all calculations
   - Return OrderResult with Decimal fields, NOT float

2. Update execution_service_v2.py:
   - OrderRequest must store Decimal fields, not float
   - All PnL calculations use Decimal arithmetic
   - No intermediate float conversions
   - Add calculate_profit_loss() function with type checking

3. Create PrecisionMonitor class in logs/precision_monitor.py:
   - Check equity every transaction
   - Alert if decimal precision exceeds 8 places
   - Log WARNING if excess decimals > 0.01 USD
   - Raise PrecisionError if corruption detected

4. Update database schema (database/schema.sql):
   - entry_price: REAL → DECIMAL(10, 8)
   - exit_price: REAL → DECIMAL(10, 8)
   - filled_price: REAL → DECIMAL(10, 8)
   - All money fields use DECIMAL, NOT REAL

5. Update AsyncLedger::execute() to convert all DECIMAL columns to Python Decimal on read

TESTING:
- Write test_decimal_precision.py with:
  * Test that Decimal(str(0.1)) + Decimal(str(0.2)) == Decimal('0.3')
  * Test that place_order() rejects float inputs, accepts only Decimal after conversion
  * Test that PrecisionMonitor raises PrecisionError on equity > 8 decimals
  * Test that 1000 trades don't accumulate precision error > $0.01

ACCEPTANCE CRITERIA:
✅ All equity values show exactly X.XX (2 decimals) or X.XXXXXXXX (8 for positions)
✅ PrecisionMonitor logs 0 warnings in 1-hour test run
✅ Database stores DECIMAL not REAL
✅ All calculations use Decimal, never float arithmetic

DIFFICULTY: MEDIUM (3-4 hours)
RISK: HIGH if skipped (precision loss = money loss)
```

---

## PROMPT 2: IDEMPOTENCY + DEDUPLICATION

```
You are implementing idempotency to prevent duplicate orders.

CURRENT STATE (BROKEN):
- In-memory dict cache without persistence
- No database table for idempotency
- Cache TTL expires mid-trade
- Bot crash = cache lost = duplicates on restart
- Same idempotency_key generated multiple times (collision)

REQUIRED FIX:
1. Create idempotency_log table in schema.sql:
   - Columns: id (PK), idempotency_key (UNIQUE), order_id, correlation_id, 
     status, filled_quantity, filled_price, fees, created_at, updated_at
   - Indexes: idx_idempotency_key (UNIQUE), idx_idempotency_order

2. Update ExecutionService.__init__():
   - Keep in-memory cache (performance)
   - Add self.ledger reference for persistence
   - Remove TTL logic (idempotency is permanent)

3. Implement _get_idempotency_record():
   - Query idempotency_log for existing record
   - If found: return OrderRecord (don't place new order)
   - If not found: proceed with order placement

4. Implement _record_idempotency():
   - After successful order: INSERT into idempotency_log
   - Store: idempotency_key, order_id, correlation_id, status='PENDING'

5. Fix idempotency_key generation (currently weak):
   - Replace hash-based (collision-prone) with UUID v5
   - Use: uuid.uuid5(NAMESPACE_DNS, f"{market_id}:{side}:{quantity}:{price}")
   - Same logical trade ALWAYS generates same UUID (deterministic)
   - Different trades generate different UUIDs (collision-proof)

6. Update place_order() signature:
   - Add parameter: idempotency_key: Optional[str] = None
   - If not provided: generate it
   - Always check _get_idempotency_record() first
   - If exists: return cached result + log 'order_deduplicated'
   - If not: proceed, then call _record_idempotency()

7. Update transaction wrapping:
   - Wrap record_trade() in database transaction
   - Automatic rollback on exception
   - Ensures atomicity: all-or-nothing

TESTING:
- Write test_idempotency.py:
  * Test 1: Place order, get order_id=ABC123
  * Test 2: Place SAME order again (same idempotency_key), verify returns same ABC123
  * Test 3: Verify ONLY ONE transaction in ledger (not two)
  * Test 4: Simulate crash (close bot), restart, re-submit same order, verify no duplicate
  * Test 5: Verify database survives restart, cache doesn't (but works because DB)

ACCEPTANCE CRITERIA:
✅ No duplicate orders ever placed
✅ Second order with same key returns same order_id
✅ Ledger shows exactly 1 transaction per logical order
✅ Bot crash + restart doesn't create duplicates
✅ idempotency_log table is populated, indexed, and queried

DIFFICULTY: MEDIUM (2-3 hours)
RISK: CRITICAL if skipped (duplicate orders = liquidation risk)
```

---

## PROMPT 3: EQUITY TRACKING CORRECTION

```
You are fixing double-entry accounting so equity calculation works correctly.

CURRENT STATE (BROKEN):
- Equity ALWAYS shows 10000.00
- Position recorded but Cash not debited
- No balance change after 100 trades
- Position cost calculated but not reflected in equity
- Asset balance seems "stuck"

ROOT CAUSE ANALYSIS:
1. record_trade() inserts into positions table BUT
2. NEVER inserts corresponding transaction_lines (journal entries)
3. account balances are NEVER updated
4. get_equity() sums accounts.balance (which never changed)

REQUIRED FIX:
1. Implement proper record_trade() with double-entry accounting:
   Step A: INSERT into transactions (journal header)
   Step B: INSERT into transaction_lines x2:
           - DEBIT Positions account (asset increases): +position_value
           - CREDIT Cash account (asset decreases): -position_value
   Step C: The database TRIGGER automatically updates accounts.balance
   Step D: get_equity() now returns correct balance

2. Update database/schema.sql - ensure triggers exist:
   - trg_update_account_balance_insert: On INSERT transaction_lines, UPDATE accounts.balance
   - Verify syntax is correct (SQLite vs PostgreSQL differences)

3. Rewrite record_trade() method:
   ```python
   async def record_trade(self, order: Order) -> None:
       correlation_id = str(uuid.uuid4())
       
       async with self.ledger.transaction():
           # Step 1: Get transaction_id
           txn_id = await self.ledger.execute(
               "INSERT INTO transactions (description, strategy, reference_id, timestamp) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
               (f"Trade {order.side} {order.quantity}@{order.price}", 
                order.strategy, correlation_id)
           )
           txn_id = await self.ledger.last_insert_row_id()
           
           # Step 2: Calculate position value
           position_value = Decimal(order.quantity) * Decimal(order.price)
           
           # Step 3: Get account IDs
           pos_account_id = await self.ledger.execute_scalar(
               "SELECT id FROM accounts WHERE account_name='Positions'"
           )
           cash_account_id = await self.ledger.execute_scalar(
               "SELECT id FROM accounts WHERE account_name='Cash'"
           )
           
           # Step 4: DEBIT Positions (asset increases)
           await self.ledger.execute(
               "INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (?, ?, ?)",
               (txn_id, pos_account_id, position_value)
           )
           
           # Step 5: CREDIT Cash (asset decreases)
           await self.ledger.execute(
               "INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (?, ?, ?)",
               (txn_id, cash_account_id, -position_value)
           )
           
           # Step 6: Insert position record
           await self.ledger.execute(
               "INSERT INTO positions (...) VALUES (...)",
               (order.market_id, order.token_id, ..., txn_id)
           )
   ```

4. Update get_equity() to verify balance:
   ```python
   async def get_equity(self) -> Decimal:
       # Calculate from transactions (source of truth)
       txn_balance = await self.ledger.execute_scalar(
           "SELECT COALESCE(SUM(amount), 0) FROM transaction_lines tl "
           "JOIN accounts a ON tl.account_id=a.id WHERE a.account_type='ASSET'"
       )
       
       # Get stored balance (should match)
       stored_balance = await self.ledger.execute_scalar(
           "SELECT COALESCE(SUM(balance), 0) FROM accounts WHERE account_type='ASSET'"
       )
       
       # Verify match
       if abs(Decimal(txn_balance) - Decimal(stored_balance)) > Decimal('0.01'):
           logger.error("EQUITY MISMATCH", extra={
               'calculated': txn_balance,
               'stored': stored_balance
           })
       
       return Decimal(txn_balance)
   ```

5. Add audit log entries:
   - Every INSERT transaction → audit_log entry
   - Entity_type='TRANSACTION', entity_id=transaction_id
   - Details: JSON with before/after balances

TESTING:
- test_double_entry_accounting.py:
  * Test 1: Place order for $1000 → Asset total increases $0, Cash decreases $1000
  * Test 2: 100 orders → Equity decreases by total spend (not stuck at 10000)
  * Test 3: Verify transaction_lines count = 2 * order_count (DEBIT + CREDIT)
  * Test 4: Verify accounts.balance updated (not stale)
  * Test 5: Check audit_log populated with every trade

ACCEPTANCE CRITERIA:
✅ After trade: Cash decreases, Positions increase
✅ Total equity = Cash + Positions (not always 10000)
✅ 100 trades → Equity shows realistic changes
✅ Stored balance matches calculated balance (within $0.01)
✅ audit_log has 2 entries per trade (DEBIT + CREDIT)

DIFFICULTY: MEDIUM (2-3 hours)
RISK: CRITICAL if skipped (over-leverage = liquidation)
```

---

## PROMPT 4: ERROR SEMANTICS (ErrorCode Enum)

```
You are implementing structured error codes (not strings).

CURRENT STATE (BROKEN):
- Errors logged as strings: "Order failed"
- Can't parse programmatically
- Can't determine if retryable
- Can't classify by severity
- All errors treated same

REQUIRED FIX:
1. Create services/error_codes.py with ErrorCode enum and TradeError exception
2. Update execution_service_v2.py to catch and re-raise with codes
3. Update OrderResult dataclass with error_code field
4. Update all error logging to include error_code
5. Implement retry logic based on error codes

ACCEPTANCE CRITERIA:
✅ All errors have ErrorCode (not string)
✅ error_code.value logged in every error
✅ Retryable errors automatically retried (backoff)
✅ Permanent errors don't retry

DIFFICULTY: EASY (1-2 hours)
RISK: MEDIUM (helps debugging, not critical for operation)
```

---

## PROMPT 5: INPUT VALIDATION AT BOUNDARIES

```
You are implementing input validation to prevent invalid orders.

CURRENT STATE (BROKEN):
- No validation on API inputs
- Price could be 99.99 (accepts, places invalid order)
- Quantity could be string "hello"
- Market ID could be invalid hash
- Size could be 1B shares (OOM crash)

REQUIRED FIX:
1. Create services/validators.py with BoundaryValidator class
2. Implement validate_price(), validate_quantity(), validate_market_id(), validate_side()
3. Update place_order() to validate ALL inputs before operations
4. Write comprehensive test suite

ACCEPTANCE CRITERIA:
✅ Invalid price (0.01, 1.00) rejected before order placed
✅ Invalid quantity (0, 1B) rejected
✅ Invalid market ID (bad hex, wrong length) rejected
✅ All inputs converted to Decimal at boundary

DIFFICULTY: EASY (1-2 hours)
RISK: MEDIUM (prevents crashes and invalid orders)
```

---

## PROMPT 6: CORRELATION ID PROPAGATION

```
You are fixing correlation ID usage across all logs.

CURRENT STATE (BROKEN):
- Correlation ID generated but not consistently used
- Some logs missing it (trace broken)
- Can't follow single order through system

REQUIRED FIX:
1. Implement CorrelationContext using contextvars
2. Update logger to include correlation_id automatically
3. Update place_order() to use CorrelationContext
4. Update record_trade() to persist correlation_id
5. Update OrderResult to include correlation_id

ACCEPTANCE CRITERIA:
✅ Every log line includes correlation_id
✅ Single order = single correlation_id across all logs
✅ Correlation_id persisted to transactions table
✅ Can trace full order lifecycle using correlation_id grep

DIFFICULTY: EASY (1-2 hours)
RISK: LOW (debugging aid, no operational impact)
```

---

## QUICK REFERENCE: Fix Order

```
APPLY IN THIS ORDER:
1. Decimal Precision (foundational)
2. Idempotency (critical for safety)
3. Equity Tracking (prevents over-leverage)
4. Error Codes (enables debugging)
5. Input Validation (prevents crashes)
6. Correlation IDs (tracing)

DO NOT SKIP ANY.
```
