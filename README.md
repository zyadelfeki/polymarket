# Polymarket Trading Bot V2

## Enterprise-Grade Automated Trading System

### Features
- **Volatility Arbitrage**: Exploit panic-priced markets during BTC/ETH/SOL volatility spikes
- **Threshold Arbitrage**: Guaranteed wins when exchange prices already decided outcomes
- **Real-Time WebSocket**: Millisecond-latency price feeds from Binance
- **Adaptive Kelly Sizing**: Dynamic position sizing based on win streaks and market volatility
- **Circuit Breaker**: Automatic trading halt on drawdown/loss limits
- **Parallel Market Scanning**: Check 50+ markets in <3 seconds

### Setup

1. **Install Dependencies**
```bash
pip install -r requirements.txt
```

2. **Configure Environment**
```bash
cp .env.example .env
# Edit .env with your API keys
```

3. **Required API Keys**
- Polymarket Private Key (for trading)
- Optional: NewsAPI, Twitter (for sentiment strategies)

4. **Run Bot**
```bash
# Paper trading (recommended first)
python main.py

# Live trading (after testing)
# Set PAPER_TRADING=false in .env
python main.py
```

### Architecture
```
config/          - Settings and market definitions
data_feeds/      - Binance WebSocket + Polymarket CLOB client
strategy/        - Trading strategies (volatility, threshold, etc)
risk/            - Position sizing, circuit breaker, position manager
utils/           - Logging, database, performance tracking
```

### Safety
- **Default: Paper Trading** - Test without risking capital
- **Position Limits**: Max 20% per trade, 3 simultaneous positions
- **Circuit Breaker**: Auto-stop at 15% drawdown
- **Consecutive Loss Protection**: Halt after 3 losses
- **Daily Trade Limit**: Max 50 trades/day

### Performance Monitoring
- All trades logged to SQLite database
- Real-time P&L tracking
- Win rate and Sharpe ratio calculation
- Performance reports every 5 minutes

### Strategies

#### 1. Volatility Arbitrage
- Monitors BTC/ETH/SOL for >3% moves in 60 seconds
- Scans Polymarket for panic-priced positions (<$0.05)
- Buys discounted side, sells when volatility normalizes
- Target: 6x-12x returns per trade

#### 2. Threshold Arbitrage  
- Checks if Binance price already decided market outcome
- Example: BTC at $96K, market "BTC above $95K" still 60% YES
- Bet YES at underpriced odds before market updates
- Target: 95%+ win rate on clear outcomes

### Support
For issues or questions, open a GitHub issue.

### License
MIT