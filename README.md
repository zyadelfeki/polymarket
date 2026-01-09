# Polymarket Intelligent Trading Bot

**Advanced arbitrage and volatility trading system for Polymarket prediction markets.**

## Features

- **Volatility Arbitrage**: Exploit panic selling during BTC/ETH volatility spikes
- **News Lag Trading**: React to breaking news before market prices update
- **Whale Tracking**: Copy trades from top performing wallets
- **Fast Market Scanner**: Parallel scanning of 50+ markets in <3 seconds
- **Adaptive Kelly Sizing**: Dynamic position sizing based on performance
- **Real-time WebSocket Feeds**: Millisecond-precision price updates

## Setup

```bash
# Install dependencies
pip install -r requirements.txt
python -m textblob.download_corpora

# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Initialize database
python -m utils.db init

# Run bot
python main.py
```

## API Keys Required

1. **NewsAPI** (Free): https://newsapi.org/register
2. **Polymarket**: https://docs.polymarket.com/
3. **Twitter/X** (Optional): https://developer.twitter.com/

## Risk Warning

**Trading involves substantial risk of loss. Start with paper trading mode.**

- Never risk more than you can afford to lose
- Past performance does not guarantee future results
- This bot is for educational purposes

## Architecture

```
polymarket_bot/
├── data_feeds/        # Real-time data sources
├── intelligence/      # Sentiment & edge detection
├── strategy/          # Trading strategies
├── risk/              # Position & risk management
├── execution/         # Order execution
└── backtest/          # Performance validation
```

## Performance Targets

- **Conservative**: 15-25% monthly returns
- **Aggressive**: 50-100% monthly returns (higher risk)
- **Win Rate Target**: 58-65%
- **Max Drawdown**: <25%