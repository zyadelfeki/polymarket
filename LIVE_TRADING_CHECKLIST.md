# Live Trading Approval Checklist

## ⚠️ WARNING: REAL MONEY AT RISK

Do NOT start live trading until ALL items are completed.

---

## Prerequisites

### Wallet Setup
- [ ] Create new Polygon wallet (separate from main funds)
- [ ] Send $100+ USDC to wallet address
- [ ] Export private key (Settings > Reveal Private Key)
- [ ] Store private key in .env file: `POLYMARKET_PRIVATE_KEY=0x...`
- [ ] Test API credentials derivation

### Environment
- [ ] Set `PAPER_TRADING=false` in .env
- [ ] Verify other settings are correct (INITIAL_CAPITAL, MAX_POSITION_SIZE_PCT, etc.)
- [ ] Run `python scripts/validate_setup.py` - all checks must PASS

### Code Review
- [ ] Read STRATEGIES_EXPLAINED.md (understand each strategy)
- [ ] Read RESEARCH_FINDINGS.md (understand the research)
- [ ] Review exit times: All strategies should have 30 sec - 5 min exits
- [ ] Verify circuit breaker is enabled
- [ ] Verify risk management is enforced

---

## Paper Trading Phase (72+ hours)

### Execution
- [ ] Set `PAPER_TRADING=true` in .env
- [ ] Set `INITIAL_CAPITAL=1000.00` (test with small amount)
- [ ] Run: `python main_v2.py`
- [ ] Let it run for at least 72 hours (3 days)
- [ ] Monitor logs: `tail -f logs/bot.log`

### Monitoring

Every 12 hours, check:
```bash
# Count trades
sqlite3 data/trades.db "SELECT COUNT(*) FROM trades;"

# Calculate win rate
sqlite3 data/trades.db "SELECT COUNT(*) FILTER (WHERE roi > 0) * 100.0 / COUNT(*) FROM trades;"

# Calculate average return per trade
sqlite3 data/trades.db "SELECT AVG(roi) FROM trades;"

# Total P&L
sqlite3 data/trades.db "SELECT SUM(profit) FROM trades;"
```

### Success Criteria (Paper Trading)

Must achieve ALL of:
- [ ] **Win rate > 55%** (should be 60%+ across all strategies)
- [ ] **Profit factor > 1.5** (money won / money lost)
- [ ] **Max drawdown < 15%** (no panic trading)
- [ ] **Avg trade duration < 5 min** (proper exits)
- [ ] **10+ trades completed** (sample size)
- [ ] **No circuit breaker triggers** (proper risk management)
- [ ] **Logs show no errors** (`grep ERROR logs/bot.log` returns nothing)

### What to Look For

✅ **GOOD SIGNS:**
- Consistent string of small wins (latency arb 98% win rate)
- Whale copy trades align with whale direction
- Liquidity shock trades correlate with market moves
- Circuit breaker triggers once, then correct sizing after
- Logs show proper exits (not time-outs)

❌ **RED FLAGS:**
- Win rate < 50% (strategy broken or edge degraded)
- One loss > 5% of capital (position sizing broken)
- Holding trades > 10 minutes (exit logic broken)
- Repeated order failures (API issue)
- No trades for hours (market feed disconnected)

---

## Analysis & Adjustments

### After 72 hours of paper trading, analyze:

1. **Strategy Performance**
   ```bash
   sqlite3 data/trades.db \
   "SELECT strategy, COUNT(*), AVG(roi), SUM(profit) FROM trades GROUP BY strategy;"
   ```
   
   - Which strategy is most profitable?
   - Which has highest win rate?
   - Which should be disabled if <50% win rate?

2. **Risk Management**
   ```bash
   sqlite3 data/trades.db \
   "SELECT MAX(roi) as best, MIN(roi) as worst, AVG(ROI*-1) as avg_loss FROM trades WHERE roi < 0;"
   ```
   
   - Worst loss should be ~-1% (stop loss working)
   - Avg loss should be small

3. **Exit Timing**
   ```bash
   sqlite3 data/trades.db \
   "SELECT exit_reason, COUNT(*) FROM trades GROUP BY exit_reason;"
   ```
   
   - Most should be TARGET_HIT or WHALE_EXIT
   - Should see <10% TIME_STOP exits

### Adjustments to Make

If win rate is low:
- Increase `MIN_EDGE` thresholds (0.05 → 0.10)
- Increase `MIN_CONFIDENCE` (0.30 → 0.50)
- Disable underperforming strategy

If drawdown is high:
- Reduce `MAX_POSITION_SIZE_PCT` (20% → 10%)
- Increase stop loss tightness (-1% → -0.5%)

If trades are exiting on TIME_STOP (not TARGET_HIT):
- Reduce exit time (30 sec → 20 sec)
- Increase profit target (3% → 5%)

---

## Final Approval

### Before Switching to Live:

✅ **Paper Trading Metrics**
- [ ] Win rate: ___% (target: 60%+)
- [ ] Profit factor: _____ (target: 2.0+)
- [ ] Max drawdown: __% (target: <15%)
- [ ] Sharpe ratio: _____ (target: 1.5+)
- [ ] Avg trade duration: ___ min (target: <5 min)

✅ **Risk Management**
- [ ] Circuit breaker tested and working
- [ ] Stop losses executed at -1%
- [ ] Position sizing is appropriate
- [ ] No trader override (automated execution only)

✅ **Operational**
- [ ] Logs are clean (no errors)
- [ ] Database is populated with trades
- [ ] Monitoring setup is ready
- [ ] Kill switch tested (CTRL+C stops cleanly)

✅ **Final Review**
- [ ] I understand all 5 strategies
- [ ] I accept the risks (crypto volatility, execution risk, model risk)
- [ ] I will NOT manually override bot decisions
- [ ] I will NOT increase position sizes beyond configured limits
- [ ] I will monitor daily and halt if issues occur

---

## Go Live

### Step 1: Enable Live Trading
```bash
# Edit .env
PAPER_TRADING=false
INITIAL_CAPITAL=100.00  # Start with small amount
```

### Step 2: Start Bot
```bash
python main_v2.py
```

### Step 3: Monitor First 24 Hours
- Check logs every 1 hour
- Verify orders are being placed
- Monitor wallet USDC balance

```bash
# Watch logs
tail -f logs/bot.log

# Check recent trades
sqlite3 data/trades.db "SELECT * FROM trades ORDER BY timestamp DESC LIMIT 5;"

# Check current P&L
sqlite3 data/trades.db "SELECT SUM(profit) FROM trades WHERE timestamp > datetime('now', '-1 hour');"
```

### Step 4: Gradual Scaling

**Day 1:** $100 capital
- Verify execution works
- Confirm trades hit Polymarket
- Check exit logic

**Day 2-3:** $500 capital
- Verify scaling works
- Confirm P&L is reasonable
- Stress test circuit breaker

**Day 4+:** Scale to full capital
- Monitor daily
- Review weekly performance
- Adjust if needed

---

## Emergency Procedures

### If Something Goes Wrong

```bash
# IMMEDIATE STOP
CTRL+C

# This will:
# 1. Stop accepting new trades
# 2. Allow existing trades to exit naturally
# 3. Close database connection
# 4. Print final P&L
```

### Emergency Disable

If you need to stop trading immediately:

```bash
# Edit .env
CIRCUIT_BREAKER_ENABLED=true
MAX_DRAWDOWN_PCT=0.01  # Halt immediately

# Or disable all strategies
LATENCY_ARBITRAGE_ENABLED=false
WHALE_TRACKING_ENABLED=false
LIQUIDITY_SHOCK_ENABLED=false
ML_ENABLED=false
```

---

## Monitoring Dashboard

Create simple monitoring (optional):

```bash
#!/bin/bash
# monitor.sh

while true; do
    clear
    echo "=== POLYMARKET BOT STATUS ==="
    echo "Time: $(date)"
    echo ""
    echo "Recent trades:"
    sqlite3 data/trades.db "SELECT COUNT(*) FROM trades WHERE timestamp > datetime('now', '-1 hour') as recent_trades;"
    echo ""
    echo "Today's P&L:"
    sqlite3 data/trades.db "SELECT SUM(profit) FROM trades WHERE date(timestamp) = date('now');"
    echo ""
    echo "Win rate (last 20 trades):"
    sqlite3 data/trades.db "SELECT COUNT(*) FILTER (WHERE profit > 0) * 100.0 / COUNT(*) FROM (SELECT profit FROM trades ORDER BY timestamp DESC LIMIT 20);"
    echo ""
    sleep 60
done
```

---

## Sign-Off

**I have:**
- [ ] Completed paper trading phase (72+ hours)
- [ ] Achieved minimum success metrics
- [ ] Understood all 5 strategies
- [ ] Tested emergency procedures
- [ ] Verified wallet setup
- [ ] Read and accepted risk disclaimer
- [ ] Set up monitoring
- [ ] Approved to proceed with live trading

**Name:** _________________

**Date:** _________________

**Live Trading Start Date:** _________________

---

## Risk Disclaimer

This bot trades cryptocurrency prediction markets. These are HIGHLY volatile and carry substantial risk of loss.

- The strategies are based on historical data and may not work in future
- No guarantees of profitability
- You can lose your entire initial capital
- Past performance does not guarantee future results
- Market conditions change; edge may degrade

Only risk capital you can afford to lose.
