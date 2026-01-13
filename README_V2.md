# Polymarket Trading Bot - Production System

A production-grade algorithmic trading system for Polymarket prediction markets with institutional-quality architecture.

## 🚀 Quick Start

```powershell
# Windows PowerShell (Recommended)
.\run.ps1 -Mode paper -Capital 10000

# Or manually
.\venv\Scripts\Activate.ps1
python main_v2.py --mode paper --capital 10000
```

```bash
# Linux/Mac
./run.sh --mode paper --capital 10000

# Or manually
source venv/bin/activate
python main_v2.py --mode paper --capital 10000
```

## 📋 Prerequisites

- Python 3.11+
- Virtual environment
- Internet connection for price feeds
- (Optional) API credentials for live trading

## 🔧 Installation

### 1. Clone Repository
```bash
git clone https://github.com/zyadelfeki/polymarket.git
cd polymarket
```

### 2. Create Virtual Environment
```powershell
# Windows
python -m venv venv
.\venv\Scripts\Activate.ps1

# Linux/Mac
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. (Optional) Install Polymarket SDK for Live Trading
```bash
pip install py-clob-client
```

## 🏗️ Architecture

### Core Components

1. **Database Layer** (`database/`)
   - Async SQLite with connection pooling
   - Double-entry accounting ledger
   - Position tracking
   - Transaction audit trail

2. **Data Feeds** (`data_feeds/`)
   - Binance WebSocket (real-time BTC prices)
   - Polymarket API client (market data & orders)
   - Rate limiting and retry logic

3. **Risk Management** (`risk/`)
   - Circuit breaker (halt on drawdown)
   - Adaptive Kelly position sizing
   - Position limits enforcement

4. **Execution** (`services/`)
   - Order management
   - Fill tracking (TODO: needs completion)
   - Slippage monitoring

5. **Strategies** (`strategies/`)
   - Latency Arbitrage (CEX vs Polymarket)
   - More coming soon...

### Data Flow

```
Binance WS → BTC Price → Strategy → Signal → Execution → Polymarket
                ↓                                           ↓
            Polymarket ← Market Odds ←──────────────────────┘
                ↓
            Database Ledger ← Record Trades
                ↓
            Circuit Breaker ← Monitor Risk
```

## 📊 Trading Modes

### Paper Trading (Default)
- Simulated execution
- No real money
- Uses shared memory database
- Perfect for testing strategies

```powershell
.\run.ps1 -Mode paper -Capital 10000
```

### Live Trading
- Real API execution
- Real money at risk
- Requires credentials
- Use with caution

```powershell
.\run.ps1 -Mode live -Capital 1000
```

## ⚙️ Configuration

### Environment Variables
Create `.env` file:

```bash
# Polymarket (Required for live trading)
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=...

# Optional
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### Strategy Parameters

Edit `main_v2.py` or pass as arguments:

```python
config = {
    'mode': 'paper',              # paper or live
    'initial_capital': 10000,      # Starting capital
    'market_id': 'btc_to_100k',   # Market to trade
    'min_spread_bps': 50,          # Min 0.5% spread
    'max_spread_bps': 500,         # Max 5% spread
    'max_position_pct': 10.0,      # Max 10% per trade
    'max_drawdown_pct': 15.0,      # Circuit trips at 15%
    'poll_interval': 2.0,          # Poll every 2 seconds
}
```

## 📈 Monitoring

### Real-Time Logs
System uses structured logging (structlog):

```
2026-01-13 16:32:51 [info] strategy_started market=btc_to_100k
2026-01-13 16:32:53 [debug] binance_price_update symbol=BTC price=95432.50
2026-01-13 16:32:53 [debug] polymarket_odds_fetched yes_price=0.45
2026-01-13 16:32:54 [info] signal_generated side=BUY edge_bps=120
```

### Performance Metrics
Check every 5 minutes:

```
============================================================
PERFORMANCE REPORT
============================================================
Signals: 45
Trades: 12
Execution Rate: 26.67%
Avg Latency: 125.50ms
Open Positions: 2
============================================================
```

### Database Inspection

```sql
-- View all accounts
SELECT * FROM accounts;

-- View transactions
SELECT * FROM transactions 
ORDER BY created_at DESC 
LIMIT 10;

-- View open positions
SELECT * FROM positions 
WHERE status = 'OPEN';

-- Check equity
SELECT SUM(balance) 
FROM accounts 
WHERE account_type = 'ASSET';
```

## 🔒 Security

### Secrets Management

The system includes a production-grade secrets manager supporting:

- **Local Encrypted Storage** (Fernet AES-256)
- **AWS Secrets Manager**
- **Azure Key Vault**
- **Environment Variables** (development only)

```python
from security.secrets_manager import SecretsManager

# Local encrypted
manager = SecretsManager(
    backend="local",
    master_password="your-secure-password"
)

# AWS
manager = SecretsManager(
    backend="aws",
    region="us-east-1"
)

# Use
private_key = manager.get_secret("polymarket_private_key")
```

### Best Practices

1. **Never commit secrets to Git**
2. **Use encrypted storage for production**
3. **Rotate keys regularly**
4. **Monitor access logs**
5. **Use principle of least privilege**

## 🧪 Testing

### Run Tests
```bash
pytest tests/ -v --cov=. --cov-report=html
```

### Manual Testing Checklist

- [ ] Database initialization works
- [ ] Can connect to Binance WebSocket
- [ ] Can fetch Polymarket markets
- [ ] Orders execute (paper mode)
- [ ] Circuit breaker trips on losses
- [ ] Graceful shutdown works
- [ ] Logs are readable
- [ ] Metrics are accurate

## 🐛 Troubleshooting

### "ModuleNotFoundError: No module named 'structlog'"
**Solution:** Activate virtual environment first
```powershell
.\venv\Scripts\Activate.ps1
# Or use launcher script
.\run.ps1
```

### "No such table: accounts"
**Solution:** This should be fixed. If still occurs:
1. Delete database file: `rm data/trading.db`
2. Restart bot

### "Cannot connect to Binance"
**Check:** Internet connection, firewall settings
```powershell
Test-NetConnection stream.binance.com -Port 9443
```

### "WebSocket closed unexpectedly"
**Normal:** Auto-reconnect should handle this
**If persistent:** Check Binance API status

### "Circuit breaker OPEN"
**Cause:** Max drawdown reached (default 15%)
**Action:** Review trades, adjust strategy, restart when ready

## 📚 API Reference

### Key Classes

#### `TradingBot`
Main orchestrator
- `initialize()` - Set up all components
- `start()` - Begin trading
- `stop()` - Graceful shutdown

#### `AsyncLedger`
Database interface
- `get_equity()` - Current capital
- `record_trade_entry()` - Log new position
- `get_open_positions()` - Active trades

#### `CircuitBreakerV2`
Risk management
- `can_trade()` - Check if trading allowed
- `record_trade_result()` - Update statistics
- `get_status()` - Current state

#### `LatencyArbitrageEngine`
Trading strategy
- `start()` - Begin monitoring
- `stop()` - Halt strategy
- `get_metrics()` - Performance stats

## 🎯 Roadmap

### Phase 1: Foundation (✅ 80% Complete)
- [x] Database architecture
- [x] API clients (Binance, Polymarket)
- [x] Risk management (circuit breaker, Kelly sizing)
- [x] Basic execution service
- [ ] Order fill monitoring (in progress)
- [ ] Position reconciliation

### Phase 2: Production Hardening (🚧 In Progress)
- [ ] Comprehensive testing (unit, integration, e2e)
- [ ] Alerting system (Telegram, Discord, Email)
- [ ] Performance monitoring (Prometheus, Grafana)
- [ ] Deployment automation (Docker, CI/CD)
- [ ] Documentation completion

### Phase 3: Advanced Features (📋 Planned)
- [ ] Multiple strategies (whale tracking, liquidity shock, ML ensemble)
- [ ] Backtest framework
- [ ] Strategy optimization
- [ ] Portfolio analytics
- [ ] Web dashboard

### Phase 4: Scale (🔮 Future)
- [ ] Multi-market support
- [ ] High-frequency capabilities
- [ ] ML-powered predictions
- [ ] Custom indicators
- [ ] API for external strategies

## 💡 Contributing

This is a production trading system handling real money. Contributions require:

1. **Unit tests** (80%+ coverage)
2. **Type hints** on all functions
3. **Structured logging** for debugging
4. **Error handling** for all edge cases
5. **Documentation** (docstrings + README)

## ⚖️ License

Proprietary - Not for public distribution

## ⚠️ Disclaimer

**IMPORTANT:** This software is provided as-is. Trading involves substantial risk of loss. Never trade with money you cannot afford to lose.

- No guarantee of profitability
- Past performance ≠ future results
- Test thoroughly before live trading
- Start with small capital
- Monitor continuously

## 📞 Support

- **Issues:** Check PRODUCTION_STATUS.md first
- **Logs:** Enable debug logging: `export LOG_LEVEL=DEBUG`
- **Database:** Use SQLite browser to inspect data
- **Monitoring:** Check structured logs for errors

## 📖 Further Reading

- [COMPREHENSIVE_AUDIT_REPORT.md](COMPREHENSIVE_AUDIT_REPORT.md) - Detailed system analysis
- [PRODUCTION_STATUS.md](PRODUCTION_STATUS.md) - Current readiness status
- [STRATEGIES_EXPLAINED.md](STRATEGIES_EXPLAINED.md) - Strategy documentation
- [database/schema.sql](database/schema.sql) - Database structure

---

**Built with:** Python 3.11, AsyncIO, SQLite, WebSockets, Structlog  
**Status:** 🟡 Beta - Paper trading ready, live trading needs testing  
**Version:** 2.0.0  
**Last Updated:** January 13, 2026
