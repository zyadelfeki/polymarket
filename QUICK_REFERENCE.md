# Quick Reference Guide - Essential Commands

**Polymarket Trading Bot v1.0**  
**For:** Paper Trading & Production Operations

---

## ⚡ Quick Start (5 Minutes)

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
nano .env  # Add API keys, set PAPER_TRADING=true

# 3. Initialize
sqlite3 data/trading.db < database/schema.sql

# 4. Test
python run_tests.py  # Must show 48/48 passing

# 5. Backtest
python run_backtest.py --mock --days 7  # Must pass 5/5 criteria

# 6. Launch
python main_production.py
```

---

## 🛠️ Testing Commands

### Run All Tests
```bash
python run_tests.py
```

### Run Specific Test Module
```bash
python run_tests.py --module test_ledger
python run_tests.py --module test_kelly_sizer
```

### Verbose Output
```bash
python run_tests.py --verbose
```

### Expected Output
```
Tests Run: 48
✅ Passed: 48
❌ Failed: 0
Success Rate: 100.0%
```

---

## 📊 Backtesting Commands

### Quick Test (Mock Data)
```bash
python run_backtest.py --mock --days 7
```

### With Specific Capital
```bash
python run_backtest.py --mock --days 7 --capital 10000
```

### Real Historical Data
```bash
python run_backtest.py --start 2026-01-01 --end 2026-01-10
```

### Save Results
```bash
python run_backtest.py --mock --days 7 --output results.json
```

### Must Pass (5/5 Criteria)
- ✅ Win rate >= 55%
- ✅ Sharpe ratio >= 1.0
- ✅ Max drawdown <= 15%
- ✅ Total return > 0%
- ✅ Total trades >= 10

---

## 🚀 Deployment Commands

### Start Paper Trading (Foreground)
```bash
export PAPER_TRADING=true
python main_production.py
```

### Start Paper Trading (Background)
```bash
export PAPER_TRADING=true
nohup python main_production.py > logs/nohup.out 2>&1 &
echo $! > bot.pid
```

### Start Live Trading (After Validation)
```bash
export PAPER_TRADING=false
python main_production.py
```

### Check if Running
```bash
ps aux | grep main_production.py
# OR
ps -p $(cat bot.pid)
```

---

## 🛑 Stop Commands

### Graceful Stop (Recommended)
```bash
kill -TERM $(cat bot.pid)
```

### Force Stop (If Needed)
```bash
kill -9 $(cat bot.pid)
```

### Verify Stopped
```bash
ps -p $(cat bot.pid) || echo "Bot stopped"
```

---

## 📊 Monitoring Commands

### Real-Time Logs
```bash
tail -f logs/trading_bot.log
```

### Filter for Trades
```bash
grep "TRADE\|ENTRY\|EXIT" logs/trading_bot.log | tail -20
```

### Filter for Errors
```bash
grep -i "error\|exception\|failed" logs/trading_bot.log | tail -20
```

### Filter for Stats
```bash
grep "STATS SUMMARY" logs/trading_bot.log | tail -5
```

### Watch Stats (Auto-Refresh)
```bash
watch -n 60 'grep "STATS SUMMARY" logs/trading_bot.log | tail -1'
```

---

## 💾 Database Queries

### Current Equity
```bash
sqlite3 data/trading.db "SELECT SUM(balance) as equity FROM accounts WHERE account_type='ASSET'"
```

### Open Positions
```bash
sqlite3 data/trading.db "SELECT * FROM positions WHERE status='OPEN'"
```

### Recent Closed Positions
```bash
sqlite3 data/trading.db "SELECT market_id, entry_price, exit_price, quantity, realized_pnl FROM positions WHERE status='CLOSED' ORDER BY exit_timestamp DESC LIMIT 10"
```

### Realized PnL (Total)
```bash
sqlite3 data/trading.db "SELECT SUM(realized_pnl) as total_pnl FROM positions WHERE status='CLOSED'"
```

### Win Rate
```bash
sqlite3 data/trading.db "SELECT 
  COUNT(CASE WHEN realized_pnl > 0 THEN 1 END) * 100.0 / COUNT(*) as win_rate 
FROM positions 
WHERE status='CLOSED'"
```

### Today's Trades
```bash
sqlite3 data/trading.db "SELECT COUNT(*) FROM positions WHERE DATE(entry_timestamp) = DATE('now')"
```

### Health Status
```bash
sqlite3 data/trading.db "SELECT component_name, status, last_check FROM health_status ORDER BY last_check DESC LIMIT 5"
```

### Recent Transactions
```bash
sqlite3 data/trading.db "SELECT * FROM transactions ORDER BY timestamp DESC LIMIT 10"
```

### Verify Ledger Balance (Should be 0 rows)
```bash
sqlite3 data/trading.db "SELECT transaction_id, SUM(amount) FROM transaction_lines GROUP BY transaction_id HAVING ABS(SUM(amount)) > 0.01"
```

---

## 🚨 Emergency Commands

### Emergency Shutdown
```bash
# 1. Stop bot
kill -TERM $(cat bot.pid)

# 2. Wait 30 seconds
sleep 30

# 3. Force if needed
kill -9 $(cat bot.pid) 2>/dev/null

# 4. Check open positions
sqlite3 data/trading.db "SELECT * FROM positions WHERE status='OPEN'"
```

### Create Backup
```bash
mkdir -p backups
cp data/trading.db backups/emergency_backup_$(date +%Y%m%d_%H%M%S).db
cp logs/trading_bot.log backups/log_backup_$(date +%Y%m%d_%H%M%S).log
```

### Check System Resources
```bash
# Memory usage
ps aux | grep main_production.py | awk '{print $4}'

# Disk space
df -h data/ logs/

# Database size
du -h data/trading.db
```

---

## 🔍 Diagnostics

### Test API Connection
```bash
curl -s https://clob.polymarket.com/health
```

### Test Database
```bash
sqlite3 data/trading.db "PRAGMA integrity_check"
```

### Check Configuration
```bash
grep -E "PAPER_TRADING|KELLY_FRACTION|MAX_BET" .env
```

### Verify Python Packages
```bash
pip list | grep -E "aiohttp|py-clob-client|websockets|pandas"
```

### Check Log File Size
```bash
du -h logs/trading_bot.log
```

### Count Recent Errors
```bash
grep -i error logs/trading_bot.log | wc -l
```

---

## 📊 Performance Analysis

### Calculate Win Rate
```bash
sqlite3 data/trading.db "SELECT 
  COUNT(*) as total_trades,
  COUNT(CASE WHEN realized_pnl > 0 THEN 1 END) as wins,
  COUNT(CASE WHEN realized_pnl > 0 THEN 1 END) * 100.0 / COUNT(*) as win_rate_pct
FROM positions WHERE status='CLOSED'"
```

### Average Win/Loss
```bash
sqlite3 data/trading.db "SELECT 
  AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) as avg_win,
  AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END) as avg_loss
FROM positions WHERE status='CLOSED'"
```

### Daily PnL
```bash
sqlite3 data/trading.db "SELECT 
  DATE(exit_timestamp) as date,
  SUM(realized_pnl) as daily_pnl,
  COUNT(*) as trades
FROM positions 
WHERE status='CLOSED'
GROUP BY DATE(exit_timestamp)
ORDER BY date DESC"
```

### Hourly Trade Count
```bash
sqlite3 data/trading.db "SELECT 
  strftime('%Y-%m-%d %H:00', entry_timestamp) as hour,
  COUNT(*) as trades
FROM positions
GROUP BY hour
ORDER BY hour DESC
LIMIT 24"
```

---

## 🧹 Maintenance Commands

### Rotate Logs
```bash
gzip logs/trading_bot.log
mv logs/trading_bot.log.gz logs/archive/trading_bot_$(date +%Y%m%d).log.gz
touch logs/trading_bot.log
```

### Vacuum Database (Reduce Size)
```bash
sqlite3 data/trading.db "VACUUM;"
```

### Archive Old Data (Optional)
```bash
# Backup positions older than 30 days
sqlite3 data/trading.db "SELECT * FROM positions WHERE entry_timestamp < datetime('now', '-30 days')" > backups/old_positions.csv
```

### Clean Old Logs
```bash
find logs/archive -name "*.log.gz" -mtime +90 -delete
```

---

## 📄 Export Commands

### Export All Positions to CSV
```bash
sqlite3 -header -csv data/trading.db "SELECT * FROM positions" > positions_export.csv
```

### Export PnL Report
```bash
sqlite3 -header -csv data/trading.db "SELECT 
  market_id,
  entry_timestamp,
  exit_timestamp,
  entry_price,
  exit_price,
  quantity,
  realized_pnl
FROM positions 
WHERE status='CLOSED'
ORDER BY exit_timestamp DESC" > pnl_report.csv
```

### Export Health History
```bash
sqlite3 -header -csv data/trading.db "SELECT * FROM health_status ORDER BY last_check DESC" > health_history.csv
```

---

## 🔄 Restart Commands

### Full Restart (Safe)
```bash
# 1. Stop bot
kill -TERM $(cat bot.pid)
sleep 10

# 2. Backup
cp data/trading.db backups/pre_restart_$(date +%Y%m%d_%H%M%S).db

# 3. Verify stopped
ps -p $(cat bot.pid) && echo "Still running!" || echo "Stopped OK"

# 4. Restart
nohup python main_production.py > logs/nohup.out 2>&1 &
echo $! > bot.pid

# 5. Monitor startup
tail -f logs/trading_bot.log
```

---

## ⚙️ Configuration Quick Checks

### Verify Paper Trading Mode
```bash
echo "Paper trading: $(grep PAPER_TRADING .env | cut -d'=' -f2)"
# MUST show: true (for paper trading)
```

### Verify Safety Parameters
```bash
grep -E "MAX_BET_PCT|MAX_AGGREGATE|KELLY_FRACTION|MIN_EDGE" config/settings.py
# Should show: 5.0, 20.0, 0.25, 0.02
```

### Verify Rate Limiting
```bash
grep RATE_LIMIT .env
# Should show: 8.0 (safe under Polymarket's 10/sec)
```

---

## 📱 Alert/Notification (If Configured)

### Test Telegram Bot
```bash
python -c "
import os
import requests
bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
chat_id = os.getenv('TELEGRAM_CHAT_ID')
if bot_token and chat_id:
    requests.post(f'https://api.telegram.org/bot{bot_token}/sendMessage',
                  json={'chat_id': chat_id, 'text': 'Bot test message'})
    print('Telegram test sent')
else:
    print('Telegram not configured')
"
```

---

## 🔥 Common Issues & Fixes

### Issue: Bot Won't Start
```bash
# Check Python version
python --version  # Need 3.9+

# Check dependencies
pip install -r requirements.txt --upgrade

# Check logs
tail -50 logs/trading_bot.log
```

### Issue: No Trades
```bash
# Check if opportunities found
grep "Opportunity" logs/trading_bot.log

# Check edge threshold
grep "edge" logs/trading_bot.log | tail -10

# Check exposure
sqlite3 data/trading.db "SELECT SUM(quantity * entry_price) / (SELECT SUM(balance) FROM accounts WHERE account_type='ASSET') * 100 as exposure_pct FROM positions WHERE status='OPEN'"
```

### Issue: Database Locked
```bash
# Check for other connections
lsof data/trading.db

# Kill if needed
kill <PID>

# Verify integrity
sqlite3 data/trading.db "PRAGMA integrity_check"
```

### Issue: API Rate Limits
```bash
# Check rate limit errors
grep "rate limit\|429" logs/trading_bot.log

# Reduce rate in .env
sed -i 's/RATE_LIMIT_PER_SEC=8.0/RATE_LIMIT_PER_SEC=6.0/' .env

# Restart bot
```

---

## 📊 Dashboard (One-Liner)

### Show Everything Important
```bash
echo "=== TRADING BOT STATUS ==="
echo "Bot Running: $(ps aux | grep -v grep | grep main_production.py > /dev/null && echo 'YES' || echo 'NO')"
echo "Equity: $(sqlite3 data/trading.db 'SELECT SUM(balance) FROM accounts WHERE account_type="ASSET"')"
echo "Open Positions: $(sqlite3 data/trading.db 'SELECT COUNT(*) FROM positions WHERE status="OPEN"')"
echo "Today Trades: $(sqlite3 data/trading.db 'SELECT COUNT(*) FROM positions WHERE DATE(entry_timestamp) = DATE("now")')"
echo "Realized PnL: $(sqlite3 data/trading.db 'SELECT SUM(realized_pnl) FROM positions WHERE status="CLOSED"')"
echo "Win Rate: $(sqlite3 data/trading.db 'SELECT COUNT(CASE WHEN realized_pnl > 0 THEN 1 END) * 100.0 / COUNT(*) FROM positions WHERE status="CLOSED"')%"
echo "Recent Errors: $(grep -i error logs/trading_bot.log | tail -5 | wc -l)"
echo "========================="
```

---

## 📝 File Locations

```
polymarket/
├── .env                  # Configuration (DO NOT COMMIT)
├── data/trading.db       # Production database
├── logs/trading_bot.log  # Main log file
├── bot.pid               # Process ID file
├── main_production.py    # Bot entry point
├── run_tests.py          # Test runner
├── run_backtest.py       # Backtest runner
└── requirements.txt      # Dependencies
```

---

## ⌨️ Useful Aliases (Add to ~/.bashrc)

```bash
# Bot management
alias bot-start='cd ~/polymarket && python main_production.py'
alias bot-stop='kill -TERM $(cat ~/polymarket/bot.pid)'
alias bot-status='ps -p $(cat ~/polymarket/bot.pid)'
alias bot-logs='tail -f ~/polymarket/logs/trading_bot.log'

# Monitoring
alias bot-equity='sqlite3 ~/polymarket/data/trading.db "SELECT SUM(balance) FROM accounts WHERE account_type=\"ASSET\""
alias bot-positions='sqlite3 ~/polymarket/data/trading.db "SELECT * FROM positions WHERE status=\"OPEN\""
alias bot-pnl='sqlite3 ~/polymarket/data/trading.db "SELECT SUM(realized_pnl) FROM positions WHERE status=\"CLOSED\""

# Testing
alias bot-test='cd ~/polymarket && python run_tests.py'
alias bot-backtest='cd ~/polymarket && python run_backtest.py --mock --days 7'
```

---

## 🔗 Quick Links

- **README:** [Full documentation](README.md)
- **Deployment:** [16-step guide](DEPLOYMENT_GUIDE.md)
- **Audit:** [All fixes documented](AUDIT_REPORT.md)
- **Checklist:** [Pre-flight checklist](PRE_FLIGHT_CHECKLIST.md)
- **Summary:** [Project overview](PROJECT_SUMMARY.md)

---

## 🆘 Help & Support

### Where to Find Help
1. Check logs: `tail -f logs/trading_bot.log`
2. Review README.md
3. Check DEPLOYMENT_GUIDE.md
4. Review this quick reference

### Emergency Contact
- Repository: [github.com/zyadelfeki/polymarket](https://github.com/zyadelfeki/polymarket)
- Issues: Create GitHub issue with logs

---

**Quick Reference Version 1.0**  
**Last Updated: January 11, 2026**  
**Keep this handy during operations!** 🚀