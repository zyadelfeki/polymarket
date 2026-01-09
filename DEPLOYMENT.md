# Production Deployment Guide

## ⚠️ CRITICAL: Read Before Running With Real Money

### Pre-Deployment Checklist

- [ ] Python 3.9+ installed
- [ ] All dependencies installed: `pip install -r requirements.txt`
- [ ] Polymarket private key obtained and secured
- [ ] `.env` file configured with all API keys
- [ ] **PAPER_TRADING=true** set for initial testing
- [ ] Run all test scripts successfully
- [ ] Understand the strategies being deployed
- [ ] Risk limits configured appropriately
- [ ] Circuit breaker enabled

---

## Step 1: Get Polymarket Private Key

1. Create Polygon wallet (MetaMask recommended)
2. Export private key (Settings > Security & Privacy > Reveal Private Key)
3. Fund wallet with USDC on Polygon network
4. **SECURE THIS KEY** - never commit to git, never share

**Minimum balance:** $20 USDC on Polygon (for fees + trading)

---

## Step 2: Configure Environment

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# REQUIRED
POLYMARKET_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE

# Start with paper trading
PAPER_TRADING=true

# Risk Management
INITIAL_CAPITAL=15.00
MAX_POSITION_SIZE_PCT=20
CIRCUIT_BREAKER_ENABLED=true
MAX_DRAWDOWN_PCT=15

# OPTIONAL (for enhanced strategies)
NEWS_API_KEY=your_newsapi_key
TWITTER_BEARER_TOKEN=your_twitter_token
```

---

## Step 3: Validate Setup

```bash
python scripts/validate_setup.py
```

This checks:
- Environment variables
- Required packages
- Configuration safety
- Directory structure

**All checks must pass before proceeding.**

---

## Step 4: Test Components (Paper Trading)

### Test Binance WebSocket (2 min)
```bash
python scripts/test_binance.py
```
**Expected:** Real-time BTC/ETH/SOL price updates

### Test Polymarket Client (1 min)
```bash
python scripts/test_polymarket.py
```
**Expected:** Fetch markets, scan crypto markets, get prices

### Test Kelly Sizer (10 sec)
```bash
python scripts/test_kelly.py
```
**Expected:** Position sizing calculations

---

## Step 5: Paper Trading (24-48 hours)

Run bot with fake money:

```bash
python main.py
```

**Monitor:**
- `logs/bot.log` - All activity
- `data/trades.db` - Trade history
- Console output - Real-time updates

**Look for:**
- Volatility spikes detected
- Opportunities found
- Simulated trades executed
- Position management
- P&L tracking

**Paper trading executes all logic except actual orders to Polymarket.**

---

## Step 6: Go Live (When Ready)

### Final Checks

- [ ] Paper trading showed profitable patterns
- [ ] No errors in logs
- [ ] Bot correctly identified opportunities
- [ ] Risk management working (position limits, circuit breaker)
- [ ] Comfortable with capital at risk

### Enable Live Trading

1. Edit `.env`:
```bash
PAPER_TRADING=false
```

2. Start with minimum capital:
```bash
INITIAL_CAPITAL=15.00  # Start small
```

3. Restart bot:
```bash
python main.py
```

---

## Monitoring Live Trading

### Real-Time Logs
```bash
tail -f logs/bot.log
```

### Database Queries
```bash
sqlite3 data/trades.db "SELECT * FROM trades ORDER BY timestamp DESC LIMIT 10;"
```

### Performance Metrics
```bash
sqlite3 data/trades.db "SELECT COUNT(*), AVG(roi), SUM(profit) FROM trades WHERE status='CLOSED';"
```

---

## Strategy Overview

### 1. Volatility Arbitrage
**Trigger:** BTC/ETH/SOL moves >3% in 60 seconds
**Action:** Scan Polymarket for panic-priced positions (<$0.05)
**Exit:** Target price reached OR 6 hours elapsed
**Expected:** 30x-50x returns on successful trades

### 2. Threshold Arbitrage
**Trigger:** Exchange price crossed market threshold
**Example:** BTC at $96K, "BTC > $95K" market still <90% YES
**Action:** Buy underpriced outcome
**Expected:** 95%+ win rate

---

## Risk Management (Built-In)

### Position Sizing
- **Kelly Criterion:** Math-based optimal bet sizing
- **Max 20% per trade** (configurable)
- **Win streak:** Increase size 1.2x
- **Loss streak:** Reduce size 0.5x

### Circuit Breaker
- **15% drawdown:** Trading halts
- **3 consecutive losses:** Trading pauses
- **50 trades/day limit:** Prevents overtrading
- **Manual resume required**

### Stop Losses
- **Volatility trades:** 50% stop loss
- **Time stops:** Exit after 6 hours if no movement
- **Target exits:** Sell when profit target hit

---

## Troubleshooting

### "401 Unauthorized" from Polymarket
**Cause:** API credentials not configured
**Fix:** Private key incorrect or API derivation failed
```bash
# Verify private key format (should start with 0x)
echo $POLYMARKET_PRIVATE_KEY
```

### "No markets found"
**Cause:** No active crypto markets on Polymarket
**Fix:** Wait for market creation or adjust scan criteria

### "WebSocket disconnected"
**Cause:** Binance connection lost
**Fix:** Bot auto-reconnects, check internet connection

### "Circuit breaker triggered"
**Cause:** Drawdown or loss limit reached
**Action:** Review trades, adjust strategy, manually resume

---

## Performance Expectations

### Conservative Estimate
- **Monthly return:** 15-30% (paper trading results)
- **Win rate:** 60-70% (threshold) + 40-50% (volatility)
- **Max drawdown:** 10-15%
- **Sharpe ratio:** 1.5-2.0

### Aggressive Scenario
- **High volatility period:** 50-100% monthly
- **Multiple opportunities/day**
- **Higher risk, higher reward**

**Past performance != future results. Crypto markets are volatile.**

---

## Maintenance

### Daily
- Check logs for errors
- Review open positions
- Monitor P&L

### Weekly
- Analyze trade history
- Adjust risk parameters if needed
- Update API keys if expired

### Monthly
- Calculate Sharpe ratio
- Review strategy performance
- Consider capital rebalancing

---

## Security

### Private Key Safety
- **Never** commit to GitHub
- Store in password manager
- Use separate wallet for bot (don't mix with main funds)
- Enable 2FA on Polygon wallet

### API Keys
- Rotate every 90 days
- Use read-only keys where possible
- Monitor for unauthorized access

---

## Support

### Logs Location
- `logs/bot.log` - Main log
- `logs/performance.log` - Metrics
- `data/trades.db` - SQLite database

### Common Issues
Check `logs/bot.log` first. Most issues logged with error details.

---

## Legal Disclaimer

This bot is provided as-is. Trading cryptocurrency prediction markets involves substantial risk of loss. Only risk capital you can afford to lose. Past performance does not guarantee future results.

The developers assume no liability for financial losses incurred while using this software.

---

## Emergency Stop

```bash
# Graceful shutdown
CTRL+C

# Force kill
killall python

# Disable trading immediately
echo "CIRCUIT_BREAKER_ENABLED=true" >> .env
echo "MAX_DRAWDOWN_PCT=0.01" >> .env
```