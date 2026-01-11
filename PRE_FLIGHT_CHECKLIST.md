# Pre-Flight Checklist - Paper Trading Deployment

**Date:** January 11, 2026  
**Version:** 1.0.0  
**Target:** Paper Trading (No Real Money)

---

## 🚀 Deployment Readiness Checklist

Complete all items before starting paper trading. Each section must be 100% complete.

---

## Section 1: Environment Setup ✅

### 1.1 System Requirements
- [ ] Python 3.9+ installed (`python --version`)
- [ ] pip package manager working (`pip --version`)
- [ ] Git installed (`git --version`)
- [ ] SQLite3 available (`sqlite3 --version`)
- [ ] At least 1GB free disk space
- [ ] Stable internet connection

### 1.2 Repository
- [ ] Code cloned from GitHub
- [ ] On correct branch (`main`)
- [ ] No uncommitted changes
- [ ] Latest version pulled (`git pull`)

**Commands:**
```bash
git clone https://github.com/zyadelfeki/polymarket.git
cd polymarket
git status  # Should show clean working tree
```

---

## Section 2: Dependencies ✅

### 2.1 Python Packages
- [ ] requirements.txt exists
- [ ] Virtual environment created (recommended)
- [ ] All packages installed successfully
- [ ] No installation errors

**Commands:**
```bash
# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Verify installation
pip list | grep -E "aiohttp|py-clob-client|websockets|pandas"
```

**Expected packages:**
- aiohttp >= 3.9.0
- py-clob-client >= 0.20.0
- websockets >= 12.0
- pandas >= 2.1.0
- pytest >= 7.4.0

---

## Section 3: Configuration ✅

### 3.1 Environment Variables
- [ ] `.env.example` exists
- [ ] Copied to `.env` (`cp .env.example .env`)
- [ ] All required variables filled in
- [ ] API keys are valid (not placeholder text)
- [ ] `PAPER_TRADING=true` is set
- [ ] No syntax errors in .env

**Required variables:**
```bash
# Check these are NOT placeholder values
grep -E "POLYMARKET_API_KEY|PRIVATE_KEY|PAPER_TRADING" .env

# Must show:
# POLYMARKET_API_KEY=actual_key_here (not "your_api_key_here")
# PRIVATE_KEY=actual_private_key (not "your_private_key_here")
# PAPER_TRADING=true (CRITICAL: must be true)
```

### 3.2 API Credentials
- [ ] Polymarket API key obtained
- [ ] Polymarket secret obtained
- [ ] Polymarket passphrase obtained
- [ ] Ethereum private key available
- [ ] Credentials tested (can authenticate)

**Test API connection:**
```bash
python -c "
from data_feeds.polymarket_client import PolymarketClient
import asyncio

async def test():
    client = PolymarketClient()
    markets = await client.get_markets(limit=1)
    print('API connection: OK' if markets else 'API connection: FAILED')

asyncio.run(test())
"
```

### 3.3 Configuration Values
- [ ] `INITIAL_CAPITAL` set (e.g., 10000.00)
- [ ] `KELLY_FRACTION` = 0.25 (conservative)
- [ ] `MAX_BET_PCT` = 5.0 (5% max)
- [ ] `MAX_AGGREGATE_EXPOSURE` = 20.0 (20% total)
- [ ] `RATE_LIMIT_PER_SEC` = 8.0 (safe limit)
- [ ] All other defaults reviewed

---

## Section 4: Database ✅

### 4.1 Database Initialization
- [ ] `data/` directory exists
- [ ] `database/schema.sql` exists
- [ ] Database created successfully
- [ ] All tables created
- [ ] All triggers created
- [ ] No errors during creation

**Commands:**
```bash
# Create directory
mkdir -p data

# Create database
sqlite3 data/trading.db < database/schema.sql

# Verify tables
sqlite3 data/trading.db ".tables"
# Should show: accounts, health_status, positions, transaction_lines, transactions

# Verify triggers
sqlite3 data/trading.db "SELECT name FROM sqlite_master WHERE type='trigger'"
# Should show: trg_check_balanced_transaction
```

### 4.2 Database Validation
- [ ] Can connect to database
- [ ] Trigger enforcement working
- [ ] No corruption
- [ ] Backup location identified

**Test trigger:**
```bash
# This should FAIL (transaction doesn't balance)
sqlite3 data/trading.db "INSERT INTO transactions (description) VALUES ('test'); INSERT INTO transaction_lines (transaction_id, account_id, amount) VALUES (1, 1, 100.00);" 2>&1 | grep -q "balanced" && echo "Trigger working" || echo "Trigger NOT working"
```

---

## Section 5: Testing ✅

### 5.1 Unit Tests
- [ ] Test files exist (`tests/test_*.py`)
- [ ] Test runner exists (`run_tests.py`)
- [ ] All 48 tests pass (100%)
- [ ] No errors or warnings
- [ ] No skipped tests
- [ ] Test execution < 5 seconds

**Commands:**
```bash
python run_tests.py

# Must show:
# Tests Run: 48
# ✅ Passed: 48
# ❌ Failed: 0
# ⚠️  Errors: 0
# ⏭️  Skipped: 0
# Success Rate: 100.0%
```

**If tests fail, STOP. Fix before proceeding.**

### 5.2 Specific Test Validation
- [ ] Ledger tests pass (19/19)
- [ ] Kelly sizer tests pass (29/29)
- [ ] Transaction balancing works
- [ ] Equity calculation correct
- [ ] Position sizing safe

**Run specific tests:**
```bash
python run_tests.py --module test_ledger --verbose
python run_tests.py --module test_kelly_sizer --verbose
```

---

## Section 6: Backtesting ✅

### 6.1 Backtest Execution
- [ ] Backtest runner exists (`run_backtest.py`)
- [ ] Mock data generation works
- [ ] Backtest completes without errors
- [ ] Results generated
- [ ] Metrics calculated

**Commands:**
```bash
python run_backtest.py --mock --days 7 --capital 10000

# Should complete in < 10 seconds
# Should generate comprehensive output
```

### 6.2 Production Criteria
- [ ] Win rate >= 55% ✅
- [ ] Sharpe ratio >= 1.0 ✅
- [ ] Max drawdown <= 15% ✅
- [ ] Total return > 0% ✅
- [ ] Total trades >= 10 ✅

**All 5 criteria must pass. Output should show:**
```
✅ STRATEGY PASSED ALL CHECKS - APPROVED FOR PAPER TRADING
```

**If backtest fails criteria, STOP. Review strategy before proceeding.**

---

## Section 7: Logging ✅

### 7.1 Log Directory
- [ ] `logs/` directory exists
- [ ] Directory is writable
- [ ] Sufficient disk space (>100MB)
- [ ] Log rotation configured (optional)

**Commands:**
```bash
mkdir -p logs
touch logs/trading_bot.log
ls -lh logs/
```

### 7.2 Log Configuration
- [ ] `LOG_LEVEL` set (INFO recommended)
- [ ] `LOG_DIR` points to correct location
- [ ] `LOG_FILE` name configured
- [ ] Log format validated

---

## Section 8: Network & Connectivity ✅

### 8.1 API Endpoints
- [ ] Can reach Polymarket API (https://clob.polymarket.com)
- [ ] Can reach Binance WebSocket (wss://stream.binance.com:9443)
- [ ] No firewall blocking
- [ ] DNS resolution working

**Test connectivity:**
```bash
# Test Polymarket API
curl -s https://clob.polymarket.com/health | grep -q "ok" && echo "Polymarket: OK" || echo "Polymarket: FAILED"

# Test Binance WebSocket
wscat -c wss://stream.binance.com:9443/ws/btcusdt@trade -x 'ping' && echo "Binance: OK" || echo "Binance: FAILED"
```

### 8.2 Rate Limiting
- [ ] Rate limit set to 8 req/sec (safe)
- [ ] Retry logic enabled
- [ ] Timeouts configured (10s)
- [ ] Token bucket working

---

## Section 9: Safety Verification ✅

### 9.1 Paper Trading Mode
- [ ] `PAPER_TRADING=true` in .env ✅✅✅
- [ ] Verified 3 times (CRITICAL)
- [ ] No real money at risk
- [ ] Understand no real trades will execute

**Triple check:**
```bash
echo "Paper trading mode: $(grep PAPER_TRADING .env | cut -d'=' -f2)"
# MUST show: true

# If shows "false", IMMEDIATELY change to "true"
sed -i 's/PAPER_TRADING=false/PAPER_TRADING=true/' .env
```

### 9.2 Safety Parameters
- [ ] Max bet = 5% (not 20%)
- [ ] Max aggregate = 20%
- [ ] Kelly fraction = 0.25 (1/4 Kelly)
- [ ] Min edge = 2%
- [ ] All caps enforced

**Verify:**
```bash
grep -E "MAX_BET_PCT|MAX_AGGREGATE|KELLY_FRACTION|MIN_EDGE" config/settings.py
```

### 9.3 Capital Settings
- [ ] `INITIAL_CAPITAL` appropriate for testing
- [ ] Using test amount (e.g., $10,000 paper money)
- [ ] Not using production capital values

---

## Section 10: Monitoring Setup ✅

### 10.1 Monitoring Tools
- [ ] Can tail logs (`tail -f logs/trading_bot.log`)
- [ ] Can query database
- [ ] Terminal multiplexer available (tmux/screen - optional)
- [ ] Monitoring commands documented

### 10.2 Health Checks
- [ ] Know how to check bot status
- [ ] Know how to check equity
- [ ] Know how to check positions
- [ ] Know how to check health status

**Key monitoring commands:**
```bash
# Real-time logs
tail -f logs/trading_bot.log

# Current equity
sqlite3 data/trading.db "SELECT SUM(balance) FROM accounts WHERE account_type='ASSET'"

# Open positions
sqlite3 data/trading.db "SELECT * FROM positions WHERE status='OPEN'"

# Health status
sqlite3 data/trading.db "SELECT * FROM health_status ORDER BY last_check DESC LIMIT 5"
```

---

## Section 11: Emergency Procedures ✅

### 11.1 Shutdown Plan
- [ ] Know how to stop bot gracefully
- [ ] Know how to force kill if needed
- [ ] Know how to check if stopped
- [ ] Backup plan documented

**Emergency shutdown:**
```bash
# Get bot PID
ps aux | grep main_production.py

# Graceful shutdown (preferred)
kill -TERM <PID>

# Force kill (if needed)
kill -9 <PID>

# Verify stopped
ps aux | grep main_production.py
```

### 11.2 Backup Plan
- [ ] Know database backup location
- [ ] Can create backup manually
- [ ] Can restore from backup
- [ ] Logs are preserved

**Create backup:**
```bash
mkdir -p backups
cp data/trading.db backups/trading_backup_$(date +%Y%m%d_%H%M%S).db
```

---

## Section 12: Documentation Review ✅

### 12.1 Documentation Availability
- [ ] README.md read and understood
- [ ] DEPLOYMENT_GUIDE.md reviewed
- [ ] AUDIT_REPORT.md reviewed (optional)
- [ ] Know where to find help

### 12.2 Key Concepts Understood
- [ ] Understand double-entry ledger
- [ ] Understand Kelly criterion
- [ ] Understand rate limiting
- [ ] Understand health monitoring
- [ ] Understand paper trading vs live

---

## Section 13: Paper Trading Plan ✅

### 13.1 Monitoring Schedule
- [ ] Plan to monitor first hour continuously
- [ ] Plan to check every 4 hours for first 24h
- [ ] Plan to review daily for 3 days
- [ ] Alert mechanism in place (optional)

### 13.2 Success Criteria
- [ ] Understand 72-hour minimum
- [ ] Know validation metrics:
  - Win rate >= 50%
  - Max drawdown <= 15%
  - PnL >= -2%
  - No critical errors

### 13.3 Data Collection
- [ ] Plan to record observations
- [ ] Plan to analyze trades
- [ ] Plan to review logs
- [ ] Plan to generate report

---

## Section 14: Final Verification ✅

### 14.1 Pre-Launch Checklist
- [ ] All above sections completed (100%)
- [ ] All tests passing (48/48)
- [ ] Backtest passed (5/5 criteria)
- [ ] Paper trading mode confirmed
- [ ] Monitoring ready
- [ ] Emergency procedures understood

### 14.2 Launch Readiness
- [ ] Ready to start bot
- [ ] Ready to monitor continuously
- [ ] Ready to respond to issues
- [ ] Ready to stop if needed

---

## 🚀 Launch Commands

### Start Paper Trading Bot

```bash
# Final verification
echo "Paper trading: $(grep PAPER_TRADING .env | cut -d'=' -f2)"
# MUST show: true

# Start bot (foreground - for testing)
python main_production.py

# OR start in background (for extended runs)
nohup python main_production.py > logs/nohup.out 2>&1 &
echo $! > bot.pid

# Monitor startup
tail -f logs/trading_bot.log

# Should see:
# [2026-01-11 18:00:00] INFO: Starting Production Trading Bot
# [2026-01-11 18:00:00] INFO: Mode: PAPER TRADING (no real money)
# [2026-01-11 18:00:01] INFO: Initial Capital: $10,000.00
# [2026-01-11 18:00:01] INFO: All systems operational
```

---

## ✅ Completion Verification

### All Sections Complete?

- [ ] Section 1: Environment Setup (6 items)
- [ ] Section 2: Dependencies (5 items)
- [ ] Section 3: Configuration (9 items)
- [ ] Section 4: Database (6 items)
- [ ] Section 5: Testing (8 items)
- [ ] Section 6: Backtesting (7 items)
- [ ] Section 7: Logging (5 items)
- [ ] Section 8: Network (6 items)
- [ ] Section 9: Safety (9 items)
- [ ] Section 10: Monitoring (6 items)
- [ ] Section 11: Emergency (6 items)
- [ ] Section 12: Documentation (6 items)
- [ ] Section 13: Paper Trading Plan (7 items)
- [ ] Section 14: Final Verification (6 items)

**Total: 92 checklist items**

---

## 🎯 GO / NO-GO Decision

### GO Criteria (ALL must be YES)

1. ✅ All 48 tests passing?
2. ✅ Backtest passed all 5 criteria?
3. ✅ PAPER_TRADING=true confirmed?
4. ✅ Database initialized correctly?
5. ✅ API credentials working?
6. ✅ Emergency procedures understood?
7. ✅ Monitoring ready?
8. ✅ All safety checks passed?

### Decision

**If ALL 8 items are YES:** ✅ **GO FOR LAUNCH**

**If ANY item is NO:** ❌ **NO-GO - Fix issues first**

---

## 📋 Post-Launch Actions

### First Hour
- [ ] Verify bot started successfully
- [ ] Check logs for errors
- [ ] Verify connections (Binance WS, Polymarket API)
- [ ] Watch for first opportunity detection
- [ ] Monitor resource usage

### First 24 Hours
- [ ] Check stats every 4 hours
- [ ] Verify trades executing (paper mode)
- [ ] Monitor PnL tracking
- [ ] Check database growth
- [ ] Review any warnings

### First 72 Hours
- [ ] Daily performance review
- [ ] Win rate calculation
- [ ] Drawdown monitoring
- [ ] Health status review
- [ ] Log analysis
- [ ] Prepare validation report

---

## 🆘 Troubleshooting Quick Reference

### Bot Won't Start
```bash
# Check Python version
python --version  # Must be 3.9+

# Check dependencies
pip list | grep py-clob-client

# Check .env file
cat .env | grep -v "^#" | grep -v "^$"

# Check logs
tail -20 logs/trading_bot.log
```

### No Trades Executing
```bash
# Check if opportunities found
grep "Opportunity" logs/trading_bot.log

# Check edge calculation
grep "edge" logs/trading_bot.log

# Check aggregate exposure
grep "exposure" logs/trading_bot.log
```

### Database Errors
```bash
# Verify database
sqlite3 data/trading.db "PRAGMA integrity_check"

# Check tables
sqlite3 data/trading.db ".tables"

# Rebuild if needed
mv data/trading.db data/trading.db.backup
sqlite3 data/trading.db < database/schema.sql
```

---

## ✅ Checklist Complete

**Completion Date:** __________________

**Verified By:** __________________

**Launch Approved:** ☐ YES  ☐ NO

**Launch Time:** __________________

---

**Remember:**
- This is paper trading (no real money)
- Monitor closely for first 72 hours
- Document all observations
- Stop immediately if critical errors occur
- Review performance before live trading

**Good luck with your paper trading deployment!** 🚀