# Quick Start (5 Minutes)

## 1. Install

```bash
git clone https://github.com/zyadelfeki/polymarket.git
cd polymarket
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
```

Edit `.env` - add your Polymarket private key:
```
POLYMARKET_PRIVATE_KEY=0xYOUR_KEY_HERE
PAPER_TRADING=true
```

## 3. Validate

```bash
python scripts/validate_setup.py
```

## 4. Test

```bash
python scripts/test_binance.py
python scripts/test_polymarket.py
python scripts/test_kelly.py
```

## 5. Run (Paper Trading)

```bash
python main.py
```

Watch the console. Bot will:
- Connect to Binance WebSocket
- Monitor BTC/ETH/SOL volatility
- Scan Polymarket for opportunities
- Execute simulated trades
- Log everything

## 6. Monitor

```bash
# Watch logs
tail -f logs/bot.log

# Check trades
sqlite3 data/trades.db "SELECT * FROM trades;"
```

## 7. Go Live (After Testing)

Edit `.env`:
```
PAPER_TRADING=false
```

Restart:
```bash
python main.py
```

---

## What It Does

### Volatility Strategy
- Detects BTC/ETH/SOL price spikes (>3% in 60s)
- Finds panic-priced Polymarket positions (<$0.05)
- Buys cheap, sells when volatility normalizes
- Target: 30x-50x returns

### Threshold Strategy
- Monitors if exchange price decided Polymarket outcome
- Example: BTC at $96K → "BTC > $95K" should be 100% YES
- Buys underpriced certain outcomes
- Target: 95%+ win rate

---

## Safety Features

- **Paper trading default** - test without risk
- **Circuit breaker** - stops at 15% drawdown
- **Position limits** - max 20% per trade
- **Kelly sizing** - mathematically optimal bets
- **Stop losses** - automatic exit on 50% loss

---

## Need Help?

Read `DEPLOYMENT.md` for complete guide.

Check `logs/bot.log` for errors.

All trades logged to `data/trades.db`.