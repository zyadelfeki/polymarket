# Production Deployment Guide

**System:** Polymarket Trading Bot v1.0  
**Status:** Ready for Paper Trading  
**Date:** January 11, 2026

---

## Deployment Phases

```
Phase 1: Pre-Deployment Validation ✅ (Complete)
  └─ Unit tests, backtesting, configuration

Phase 2: Paper Trading ⏳ (Next - 72 hours)
  └─ Live data, no real money

Phase 3: Production Deployment 📋 (After validation)
  └─ Real money, monitored
```

---

## Phase 1: Pre-Deployment Validation

### Step 1: Environment Setup

```bash
# Clone repository
git clone https://github.com/zyadelfeki/polymarket.git
cd polymarket

# Install dependencies
pip install -r requirements.txt

# Create directories
mkdir -p data logs

# Configure environment
cp .env.example .env
nano .env  # Add your API keys
```

### Step 2: Configure API Keys

**Required in `.env`:**
```bash
# Polymarket API (get from https://polymarket.com/)
POLYMARKET_API_KEY=your_api_key
POLYMARKET_SECRET=your_secret_key
POLYMARKET_PASSPHRASE=your_passphrase

# Ethereum private key for signing
PRIVATE_KEY=your_private_key  # Keep secure!

# Optional: Notification settings
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### Step 3: Initialize Database

```bash
# Create database with schema
sqlite3 data/trading.db < database/schema.sql

# Verify schema created
sqlite3 data/trading.db ".tables"
# Should show: accounts, positions, transactions, transaction_lines, etc.

# Check triggers
sqlite3 data/trading.db ".schema trg_check_balanced_transaction"
# Should show double-entry validation trigger
```

### Step 4: Run Unit Tests

```bash
# Run all tests
python run_tests.py

# Expected output:
# ============================================================
# RUNNING UNIT TESTS
# ============================================================
# 
# test_deposit_transaction_balances (tests.test_ledger.TestLedger) ... ok
# test_equity_after_deposit (tests.test_ledger.TestLedger) ... ok
# ...
# 
# Ran 48 tests in 0.453s
# 
# OK
# 
# ============================================================
# TEST SUMMARY
# ============================================================
# 
# Tests Run: 48
#   ✅ Passed: 48
#   ❌ Failed: 0
#   ⚠️  Errors: 0
#   ⏭️  Skipped: 0
# 
# Success Rate: 100.0%

# ✅ Must show 100% success before proceeding
```

### Step 5: Run Backtest

```bash
# Quick validation with mock data (7 days)
python run_backtest.py --mock --days 7 --capital 10000

# Expected output:
# ============================================================
# BACKTEST RESULTS
# ============================================================
# Strategy: latency_arb
# Period: 2026-01-04 to 2026-01-11
# 
# Initial Capital: $10000
# Final Equity: $10750
# Total PnL: +$750.00
# Total Return: +7.5%
# 
# Total Trades: 87
# Winners: 52 (59.8%)
# Losers: 35
# Avg Win: +$35.50
# Avg Loss: -$18.20
# 
# Sharpe Ratio: 1.42
# Max Drawdown: 8.3%
# 
# ============================================================
# PRODUCTION READINESS EVALUATION
# ============================================================
# ✅ PASS | Win Rate: 59.8% (criterion: >= 55%)
# ✅ PASS | Sharpe Ratio: 1.42 (criterion: >= 1.0)
# ✅ PASS | Max Drawdown: 8.3% (criterion: <= 15%)
# ✅ PASS | Total Return: 7.5% (criterion: > 0%)
# ✅ PASS | Total Trades: 87 (criterion: >= 10)
# ============================================================
# 
# ✅ STRATEGY PASSED ALL CHECKS - APPROVED FOR PAPER TRADING

# ✅ Must pass ALL checks before proceeding
```

### Step 6: Configuration Validation

```bash
# Check Kelly parameters are conservative
grep -E "KELLY_FRACTION|MAX_BET_PCT|MAX_AGGREGATE" config/settings.py

# Should show:
# KELLY_FRACTION = 0.25         # ✅ 1/4 Kelly
# MAX_BET_PCT = 5.0             # ✅ 5% max
# MAX_AGGREGATE_EXPOSURE = 20.0 # ✅ 20% total

# Check rate limiting
grep "RATE_LIMIT" config/settings.py
# Should show:
# RATE_LIMIT_PER_SEC = 8.0  # ✅ Under Polymarket's 10/sec
```

**✅ Phase 1 Complete** - Ready for paper trading

---

## Phase 2: Paper Trading (72 Hours Minimum)

### Step 7: Start Paper Trading

```bash
# Set paper trading mode
export PAPER_TRADING=true

# Or add to .env
echo "PAPER_TRADING=true" >> .env

# Start bot
python main_production.py

# Expected output:
# [2026-01-11 18:30:00] INFO: Starting Production Trading Bot
# [2026-01-11 18:30:00] INFO: Mode: PAPER TRADING (no real money)
# [2026-01-11 18:30:00] INFO: Initial Capital: $10,000.00
# [2026-01-11 18:30:01] INFO: Binance WebSocket connected
# [2026-01-11 18:30:01] INFO: Health Monitor started
# [2026-01-11 18:30:01] INFO: Latency Arb Loop started
# [2026-01-11 18:30:01] INFO: Position Monitor Loop started
# [2026-01-11 18:30:01] INFO: All systems operational
```

### Step 8: Monitor Paper Trading

#### Real-Time Log Monitoring
```bash
# In separate terminal
tail -f logs/trading_bot.log

# Watch for:
# - Opportunities found
# - Trades executed
# - Position exits
# - Health status
# - No errors/warnings
```

#### Check Stats (Every Hour)
```bash
# Stats are logged every 60 seconds
grep "STATS SUMMARY" logs/trading_bot.log | tail -5

# Example output:
# [2026-01-11 19:30:00] STATS SUMMARY:
#   Current Equity: $10,150.00
#   Open Positions: 1
#   Today's Trades: 8
#   Win Rate: 62.5%
#   Realized PnL: +$150.00 (+1.5%)
#   Health Status: ALL_SYSTEMS_OPERATIONAL
```

#### Verify Database
```bash
# Check equity from ledger
sqlite3 data/trading.db "SELECT SUM(balance) FROM accounts WHERE account_type = 'ASSET'"

# Check position count
sqlite3 data/trading.db "SELECT COUNT(*) FROM positions WHERE status = 'OPEN'"

# Check recent trades
sqlite3 data/trading.db "SELECT * FROM positions WHERE status = 'CLOSED' ORDER BY exit_timestamp DESC LIMIT 5"
```

### Step 9: Validation Checks (Every 24 Hours)

**Day 1 Checklist:**
- [ ] Bot running without crashes
- [ ] Trades being executed
- [ ] PnL tracking correctly
- [ ] No health alerts
- [ ] Position limits respected (5% per trade, 20% total)
- [ ] Rate limiting working (no API errors)
- [ ] Ledger balanced (run validation query)

```bash
# Validate ledger balancing
sqlite3 data/trading.db "SELECT transaction_id, SUM(amount) as balance FROM transaction_lines GROUP BY transaction_id HAVING ABS(SUM(amount)) > 0.01"
# Should return 0 rows (✅ all transactions balanced)
```

**Day 2 Checklist:**
- [ ] Performance metrics acceptable:
  - Win rate >= 50%
  - Max drawdown < 10%
  - No position held > 60 seconds
- [ ] Health monitor working (check for recovery from failures)
- [ ] Memory usage stable (<200 MB)
- [ ] Database size reasonable (<50 MB)

**Day 3 Checklist:**
- [ ] 72-hour uptime achieved
- [ ] Overall PnL positive or near breakeven
- [ ] All safety systems tested:
  - Time stops triggered
  - Target profits hit
  - Stop losses activated
- [ ] No data inconsistencies
- [ ] Ready for live trading

### Step 10: Paper Trading Analysis

```bash
# Generate full report
python -c "
import sqlite3
conn = sqlite3.connect('data/trading.db')

# Get all closed positions
cursor = conn.execute('SELECT * FROM positions WHERE status = \"CLOSED\"')
positions = cursor.fetchall()

print(f'Total trades: {len(positions)}')

winners = [p for p in positions if float(p[10]) > 0]  # realized_pnl column
print(f'Winners: {len(winners)} ({len(winners)/len(positions)*100:.1f}%)')

total_pnl = sum(float(p[10]) for p in positions)
print(f'Total PnL: ${total_pnl:+.2f}')

conn.close()
"
```

**Approval Criteria for Live Trading:**
- ✅ 72+ hours continuous operation
- ✅ Win rate >= 50%
- ✅ Max drawdown <= 15%
- ✅ Total PnL >= -2% (near breakeven minimum)
- ✅ No critical errors in logs
- ✅ All safety systems working
- ✅ Ledger validated (all transactions balanced)

---

## Phase 3: Production Deployment

### Step 11: Pre-Production Checklist

**❗ CRITICAL - Review before proceeding:**

```bash
# 1. Backup paper trading database
cp data/trading.db data/trading_paper_backup.db

# 2. Review all configuration
cat config/settings.py | grep -E "CAPITAL|KELLY|MAX_BET|RATE_LIMIT"

# 3. Verify API keys are for PRODUCTION (not testnet)
grep "POLYMARKET_API" .env

# 4. Set initial capital (start small!)
# Edit config/settings.py:
# INITIAL_CAPITAL = 1000.00  # Start with $1,000

# 5. Disable paper trading mode
sed -i 's/PAPER_TRADING=true/PAPER_TRADING=false/' .env
```

### Step 12: Fund Account

```bash
# Transfer USDC to your Polymarket account
# Use Polymarket UI at https://polymarket.com/

# Verify balance via API
python -c "
from data_feeds.polymarket_client import PolymarketClient
import asyncio

async def check_balance():
    client = PolymarketClient()
    balance = await client.get_balance()
    print(f'Account balance: ${balance}')

asyncio.run(check_balance())
"
```

### Step 13: Initialize Production Database

```bash
# Create fresh production database
rm data/trading.db  # Remove paper trading data
sqlite3 data/trading.db < database/schema.sql

# Record initial deposit
python -c "
from database.ledger import Ledger
from decimal import Decimal

ledger = Ledger(db_path='data/trading.db')
ledger.record_deposit(amount=Decimal('1000.00'))  # Match INITIAL_CAPITAL

print(f'Initial equity: ${ledger.get_equity()}')
"

# Should output: Initial equity: $1000.00
```

### Step 14: Start Production Trading

```bash
# Clear logs
rm logs/*.log

# Start in background with nohup
nohup python main_production.py > logs/nohup.out 2>&1 &

# Get PID
echo $! > bot.pid

# Verify running
ps -p $(cat bot.pid)

# Monitor startup
tail -f logs/trading_bot.log

# Should see:
# [2026-01-11 20:00:00] INFO: Starting Production Trading Bot
# [2026-01-11 20:00:00] INFO: Mode: LIVE TRADING (real money)
# [2026-01-11 20:00:00] WARNING: Live trading enabled - trades will use real capital
# [2026-01-11 20:00:01] INFO: Initial Capital: $1,000.00
# [2026-01-11 20:00:01] INFO: All systems operational
```

### Step 15: Intensive Monitoring (First 24 Hours)

**Hour 1: Watch every trade**
```bash
# Real-time trade monitoring
watch -n 5 'grep "TRADE\|ENTRY\|EXIT" logs/trading_bot.log | tail -10'

# Check equity every 5 minutes
watch -n 300 'sqlite3 data/trading.db "SELECT SUM(balance) FROM accounts WHERE account_type = '"'"'ASSET'"'"'""
```

**Hour 2-6: Monitor stats**
```bash
# Check stats every 30 minutes
watch -n 1800 'grep "STATS SUMMARY" logs/trading_bot.log | tail -1'
```

**Hour 6-24: Check for anomalies**
```bash
# Check for errors
grep -i "error\|exception\|failed" logs/trading_bot.log | tail -20

# Check health alerts
grep "HEALTH ALERT" logs/trading_bot.log

# If any issues, investigate immediately
```

### Step 16: Ongoing Operations

**Daily Tasks:**
```bash
# 1. Check PnL
sqlite3 data/trading.db "SELECT SUM(realized_pnl) FROM positions WHERE status = 'CLOSED'"

# 2. Review logs
grep -E "ERROR|WARNING" logs/trading_bot.log | tail -50

# 3. Verify bot running
ps -p $(cat bot.pid) || echo "Bot not running!"

# 4. Check database size
du -h data/trading.db

# 5. Backup database
cp data/trading.db backups/trading_$(date +%Y%m%d).db
```

**Weekly Tasks:**
- Review performance metrics (win rate, Sharpe, drawdown)
- Analyze losing trades
- Check for strategy degradation
- Update configuration if needed
- Review health monitor alerts

---

## Emergency Procedures

### Emergency Shutdown

```bash
# Graceful shutdown
kill -TERM $(cat bot.pid)

# Wait 30 seconds for positions to close
sleep 30

# Force kill if still running
kill -9 $(cat bot.pid) 2>/dev/null

# Verify stopped
ps -p $(cat bot.pid) || echo "Bot stopped"

# Check open positions
sqlite3 data/trading.db "SELECT * FROM positions WHERE status = 'OPEN'"

# Manually close positions on Polymarket UI if needed
```

### Critical Alerts

**PnL Drawdown > 10%:**
```bash
# 1. Stop bot immediately
kill -TERM $(cat bot.pid)

# 2. Check what happened
grep "realized_pnl" logs/trading_bot.log | tail -50

# 3. Analyze losing trades
sqlite3 data/trading.db "SELECT * FROM positions WHERE realized_pnl < 0 ORDER BY realized_pnl ASC LIMIT 10"

# 4. Fix issue before restarting
# 5. Consider reducing position sizes
```

**Health Monitor Alerts:**
```bash
# Check component status
grep "HEALTH ALERT" logs/trading_bot.log

# Common issues:
# - Binance WS disconnected: Usually auto-recovers
# - Polymarket API errors: Check API status
# - Database errors: Check disk space

# Bot should auto-recover, but monitor closely
```

**API Rate Limit Errors:**
```bash
# Check for rate limit errors
grep "rate limit\|429" logs/trading_bot.log

# If frequent:
# 1. Reduce RATE_LIMIT_PER_SEC in config/settings.py
# 2. Restart bot
# 3. Monitor for improvement
```

---

## Rollback Procedure

**If something goes wrong in production:**

```bash
# 1. IMMEDIATE: Stop bot
kill -TERM $(cat bot.pid)

# 2. Close all open positions manually
# Use Polymarket UI

# 3. Calculate final PnL
sqlite3 data/trading.db "SELECT SUM(realized_pnl) FROM positions"

# 4. Backup production database
cp data/trading.db backups/production_failure_$(date +%Y%m%d_%H%M%S).db

# 5. Restore paper trading mode
sed -i 's/PAPER_TRADING=false/PAPER_TRADING=true/' .env

# 6. Investigate root cause
grep -E "ERROR|EXCEPTION" logs/trading_bot.log > incident_report.txt

# 7. Fix issues
# 8. Re-run backtests
# 9. Re-run paper trading
# 10. Only redeploy after validation
```

---

## Success Metrics

**First Week:**
- PnL: -2% to +5% (acceptable range)
- Win rate: 50-60%
- Max drawdown: <10%
- Uptime: 99%+

**First Month:**
- PnL: +5% to +15%
- Win rate: 55-65%
- Sharpe ratio: 1.0-2.0
- Max drawdown: <15%

**Long Term:**
- Annual return: 20-50%
- Sharpe ratio: >1.5
- Max drawdown: <20%
- Consistent profitability

---

## Support & Maintenance

### Log Rotation
```bash
# Add to crontab (daily at midnight)
0 0 * * * cd /path/to/polymarket && gzip logs/trading_bot.log && mv logs/trading_bot.log.gz logs/archive/trading_bot_$(date +\%Y\%m\%d).log.gz && touch logs/trading_bot.log
```

### Database Maintenance
```bash
# Weekly vacuum (reduce database size)
sqlite3 data/trading.db "VACUUM;"

# Monthly backup
cp data/trading.db backups/monthly/trading_$(date +%Y%m).db
```

### Version Updates
```bash
# Before updating:
# 1. Stop bot
# 2. Backup database
# 3. Run tests on new version
# 4. Paper trade new version
# 5. Deploy to production
```

---

## Conclusion

**Deployment Status:**
- ✅ Phase 1: Pre-Deployment Validation - COMPLETE
- ⏳ Phase 2: Paper Trading - READY TO START
- 📋 Phase 3: Production Deployment - PENDING

**Next Steps:**
1. Complete paper trading (72 hours)
2. Validate all metrics
3. Get approval for production
4. Deploy with small capital ($1,000)
5. Scale up gradually

**Timeline:**
- Today: Complete pre-deployment validation
- Days 1-3: Paper trading
- Day 4: Production deployment
- Week 1: Intensive monitoring
- Month 1: Performance validation

**Remember:**
- Start small
- Monitor closely
- Be patient
- Adjust conservatively
- Preserve capital

**Built with production-grade safety systems. Deploy with confidence, but always monitor closely.**