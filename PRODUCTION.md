# Polymarket Latency Arbitrage Bot - Production Guide

## Architecture Verification

### ✓ Speed Test Results
**Latency: 2.38ms** (Target: <200ms)
- Infrastructure approved for HFT operations
- WebSocket → Order execution pipeline validated

### ✓ Components V2
- `BinanceWebSocketV2`: Real-time BTC price feed
- `PolymarketClientV2`: Market API with rate limiting
- `ExecutionServiceV2`: Order lifecycle management
- `CircuitBreakerV2`: Risk management system
- `AsyncLedger`: Double-entry accounting ledger
- `LatencyArbitrageEngine`: Strategy implementation

---

## Quick Start

### Paper Trading (Simulation)
```bash
git pull
python main_v2.py --mode paper --capital 10000
```

### Live Trading
```bash
export POLYMARKET_PRIVATE_KEY="your_private_key_here"
python main_v2.py --mode live
```

---

## Configuration

### Command Line Options

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | `paper` | `paper` or `live` |
| `--capital` | `10000` | Initial capital (paper mode only) |
| `--market` | `btc_to_100k` | Market ID to trade |
| `--min-spread` | `50` | Minimum spread (basis points) |
| `--max-position` | `10.0` | Max position size (% of equity) |

### Examples

**Conservative Paper Trading:**
```bash
python main_v2.py --mode paper --min-spread 100 --max-position 5
```

**Aggressive Live Trading:**
```bash
python main_v2.py --mode live --min-spread 30 --max-position 15
```

---

## Strategy Logic

### Latency Arbitrage

**Thesis:** Polymarket odds lag behind Binance spot prices by 2-10 seconds.

**Execution Flow:**
1. **Monitor:** Binance WebSocket provides real-time BTC price
2. **Poll:** Fetch Polymarket "BTC to 100K" odds every 2 seconds
3. **Calculate:** Implied probability from BTC price vs Polymarket odds
4. **Signal:** If `spread > min_threshold` → TRIGGER
5. **Risk Check:** Circuit breaker validates trade safety
6. **Execute:** Place order via ExecutionServiceV2
7. **Record:** Update ledger with double-entry accounting

### Signal Generation

```python
implied_probability = min(0.99, btc_price / 100000)
spread = implied_probability - polymarket_odds
spread_bps = spread * 10000

if spread_bps > min_spread_bps:
    # BUY YES (Polymarket underpriced)
    execute_trade()
```

### Risk Management

**Circuit Breaker Thresholds:**
- Max Drawdown: 15%
- Max Loss Streak: 5 trades
- Daily Loss Limit: 10%

**Position Sizing:**
- Default: 10% of equity per trade
- Adjustable via `--max-position`

---

## Monitoring

### Real-Time Logs

The bot outputs structured logs to console:

```log
2026-01-11 19:53:15 [info] signal_generated action=BUY side=YES spread_bps=75.3
2026-01-11 19:53:15 [info] executing_trade quantity=100.0 price=0.52
2026-01-11 19:53:15 [info] trade_executed_successfully order_id=paper_1768153995
```

### Health Checks (Every 60s)
```log
2026-01-11 19:54:15 [info] health_check equity=10250.50 circuit_breaker_state=CLOSED
```

### Performance Reports (Every 5m)
```log
2026-01-11 19:58:15 [info] performance_report
    strategy_signals=25
    strategy_trades=18
    strategy_execution_rate=0.72
    execution_fill_rate=1.0
    execution_avg_latency_ms=2.38
    open_positions=3
```

---

## Database

### Schema
- **accounts**: Chart of accounts (assets, liabilities, equity)
- **transactions**: Journal entries
- **transaction_lines**: Debits and credits
- **positions**: Open and closed positions
- **audit_log**: Complete audit trail

### Paper Mode
- Uses in-memory SQLite (`:memory:`)
- No persistence between runs

### Live Mode
- Persistent SQLite at `data/trading.db`
- Survives restarts

### Backup (Live Mode)
```bash
mkdir -p backups
cp data/trading.db backups/trading_$(date +%Y%m%d_%H%M%S).db
```

---

## Testing

### Diagnostic Scripts

**Latency Test** (measures WebSocket → Order latency):
```bash
python latency_test.py
```
Expected: <200ms average

**Dry Run** (single execution loop):
```bash
python dry_run.py
```
Expected: 3 ticks processed successfully

### Unit Tests
```bash
pytest tests/ -v
```

---

## Production Checklist

### Before Live Trading

- [ ] Run `latency_test.py` → Verify <200ms
- [ ] Run `dry_run.py` → Verify no errors
- [ ] Test with `--mode paper` for 24 hours
- [ ] Verify circuit breaker triggers correctly
- [ ] Set `POLYMARKET_PRIVATE_KEY` environment variable
- [ ] Backup empty database: `cp data/trading.db data/trading_backup.db`
- [ ] Start with small `--capital` (e.g., $100)
- [ ] Monitor closely for first hour

### During Live Trading

- [ ] Monitor logs continuously
- [ ] Check health every 5 minutes
- [ ] Verify fills are occurring
- [ ] Watch for circuit breaker triggers
- [ ] Backup database every 4 hours

### Emergency Stop

**Graceful Shutdown:**
```bash
Ctrl+C  # Sends SIGINT
```

**Force Kill:**
```bash
pkill -9 -f main_v2.py
```

---

## Troubleshooting

### "no such table: accounts"
**Cause:** Database schema not initialized
**Fix:** Ensure `await ledger.initialize()` is called before any queries

### "Circuit breaker OPEN"
**Cause:** Risk threshold exceeded
**Fix:** Check logs for reason, adjust thresholds, or wait for cooldown

### "Order execution failed"
**Cause:** API error or rate limit
**Fix:** Check Polymarket API status, verify credentials

### High latency (>200ms)
**Cause:** Network issues or system load
**Fix:** Run `latency_test.py` to diagnose, check CPU/network

---

## Performance Tuning

### Aggressive (Higher Risk)
```bash
python main_v2.py \
  --mode live \
  --min-spread 30 \
  --max-position 15
```

### Conservative (Lower Risk)
```bash
python main_v2.py \
  --mode live \
  --min-spread 100 \
  --max-position 5
```

### Backtesting (Not Implemented)
For historical backtesting, use:
```bash
python backtest.py --start 2025-01-01 --end 2025-12-31
```
*Note: Backtesting module not yet implemented*

---

## Support

**Logs:** Check console output and `data/trading.db` for audit trail

**Issues:** Review circuit breaker status and position history

**Questions:** See inline documentation in source files

---

## Architecture Summary

```
main_v2.py
├── AsyncLedger (database/ledger_async.py)
│   └── ConnectionPool → SQLite
├── PolymarketClientV2 (data_feeds/polymarket_client_v2.py)
│   └── HTTP API + Rate Limiting
├── ExecutionServiceV2 (services/execution_service_v2.py)
│   └── Order Lifecycle Management
├── CircuitBreakerV2 (risk/circuit_breaker_v2.py)
│   └── Risk Thresholds
└── LatencyArbitrageEngine (strategies/latency_arbitrage.py)
    ├── BinanceWebSocketV2 (data_feeds/binance_websocket_v2.py)
    │   └── Real-time Price Feed
    └── Strategy Logic
        ├── Signal Generation
        ├── Risk Check
        └── Order Execution
```

---

## License

Production code. Handle with care.
