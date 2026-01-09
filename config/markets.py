CRYPTO_SYMBOLS = {
    "BTC": {
        "name": "Bitcoin",
        "binance_symbol": "BTCUSDT",
        "polymarket_tags": ["bitcoin", "btc", "crypto"],
        "volatility_threshold": 3.0,
        "min_liquidity": 500
    },
    "ETH": {
        "name": "Ethereum",
        "binance_symbol": "ETHUSDT",
        "polymarket_tags": ["ethereum", "eth", "crypto"],
        "volatility_threshold": 3.5,
        "min_liquidity": 300
    },
    "SOL": {
        "name": "Solana",
        "binance_symbol": "SOLUSDT",
        "polymarket_tags": ["solana", "sol", "crypto"],
        "volatility_threshold": 4.0,
        "min_liquidity": 200
    }
}

WHALE_WALLETS = [
    "0x0000000000000000000000000000000000000000",
]

PRICE_KEYWORDS = [
    "above",
    "below",
    "reach",
    "hit",
    "close above",
    "close below",
    "exceed"
]

NEWS_KEYWORDS = {
    "high_priority": [
        "federal reserve",
        "fed decision",
        "rate cut",
        "rate hike",
        "sec",
        "regulation",
        "hack",
        "exploit",
        "emergency"
    ],
    "medium_priority": [
        "bitcoin",
        "ethereum",
        "crypto",
        "whale alert",
        "exchange",
        "listing"
    ]
}