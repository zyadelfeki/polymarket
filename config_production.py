from decimal import Decimal

STARTING_CAPITAL = Decimal("13.98")

# ---------------------------------------------------------------------------
# Charlie signal gate configuration
# These are the hard minimum thresholds that must be met before any order
# is placed.  Raise them to bet less often / with higher conviction only.
# ---------------------------------------------------------------------------
CHARLIE_CONFIG = {
    # Minimum edge (p_win - implied_prob) required for a YES or NO bet.
    # 0.05 = Charlie must believe the outcome is at least 5 percentage points
    # more likely than the market price implies.
    "min_edge": Decimal("0.05"),

    # Minimum meta-confidence from Charlie's ensemble + context fusion.
    # Range [0, 1].  0.60 ≈ "moderate conviction".
    "min_confidence": Decimal("0.60"),

    # Regimes Charlie must be in to allow betting.
    # None means all regimes are allowed.
    # Set to {"BULLISH", "NEUTRAL"} to disable bearish-regime bets.
    "allowed_regimes": None,

    # How long (seconds) to wait for Charlie's signal before aborting.
    "signal_timeout_seconds": 8.0,

    # PYTHONPATH override for the project-charlie repository root.
    # Set the CHARLIE_PATH environment variable in .env instead of hardcoding.
    # "charlie_path": "/path/to/project-charlie-main",
}

KELLY_CONFIG = {
    "fractional_kelly": Decimal("0.25"),
    "aggressive_kelly": Decimal("1.0"),
    "conservative_kelly": Decimal("0.25"),
    "growth_mode_threshold": Decimal("200.0"),
    "growth_max_bet_pct": Decimal("20.0"),
    "round_up_min_edge": Decimal("0.03"),
    "max_bet_pct": Decimal("5.0"),
    "min_edge_required": Decimal("0.02"),
    "min_confidence": Decimal("0.65"),
}

CIRCUIT_BREAKER_CONFIG = {
    "max_daily_loss": Decimal("2.00"),
    "max_drawdown_pct": Decimal("25.0"),
    "max_consecutive_losses": 5,
    "adaptive_risk_profile": True,
    "max_single_trade_loss": Decimal("0.70"),
    "cooldown_after_trip_seconds": 3600,
}

STRATEGY_CONFIG = {
    "scan_interval_seconds": 15,
    "min_market_volume": Decimal("50000"),
    "max_positions_open": 3,
    "time_window_minutes": 15,
    "min_time_to_expiry_seconds": 300,
    "min_edge": Decimal("0.02"),
}

API_CONFIG = {
    "request_timeout_seconds": 10,
    "max_retries": 3,
    "retry_delay_seconds": 2,
}

LOGGING_CONFIG = {
    "log_level": "INFO",
    "log_file": "bot_production.log",
    "max_log_size_mb": 50,
    "backup_count": 5,
    "console_output": True,
}

SAFETY_CONFIG = {
    "enable_kill_switch_check": True,
    "enable_circuit_breakers": True,
    "enable_idempotency": True,
    "decimal_precision_check": True,
    "only_buy_orders": True,
}

HEARTBEAT_FILE = "runtime/heartbeat.txt"

# ---------------------------------------------------------------------------
# Persistent order ledger
# ---------------------------------------------------------------------------
ORDER_STORE_CONFIG = {
    # SQLite database path, relative to the repo root.
    "db_path": "data/orders_ledger.db",
}

# ---------------------------------------------------------------------------
# Performance tracker — thresholds fed into the circuit breaker
# ---------------------------------------------------------------------------
PERFORMANCE_TRACKER_CONFIG = {
    # Stop trading when rolling drawdown exceeds this fraction (0 = 0 %, 1 = 100 %).
    "max_drawdown_halt": Decimal("0.15"),        # 15 % drawdown → halt

    # Stop trading when rolling win-rate (over last N trades) drops below threshold.
    "min_rolling_win_rate": Decimal("0.35"),     # < 35 % wins → halt

    # Minimum number of settled trades required before win-rate is used.
    # Below this, the win-rate filter is disabled (not enough data).
    "win_rate_min_sample": 20,
}

# ---------------------------------------------------------------------------
# Regime-based position-size multipliers
# Applied to the raw Kelly size returned by CharliePredictionGate.
# A multiplier of 1.0 = full Kelly; 0.5 = half Kelly; 0.0 = skip trade.
#
# Rationale for the defaults:
#   HIGH_VOL      → halve all bets.  Volatility is the enemy of edge; a
#                    5-pp edge in a 6-pp vol environment barely covers risk.
#   LOW_VOL       → small boost (1.1×) because spreads are tighter and our
#                    edge is more persistent.  Caps at 1.0 after Kelly cap.
#   TRENDING      → full allocation.  Our latency-arb alpha is strongest
#                    when momentum is clear.
#   MEAN_REVERTING→ moderate reduction; mean-reverting regimes are noisier
#                    for directional signals.
#   UNKNOWN       → conservative.  When we can't classify, we don't bet big.
# ---------------------------------------------------------------------------
REGIME_RISK_OVERRIDES: dict = {
    "HIGH_VOL":       Decimal("0.50"),   # halve position
    "LOW_VOL":        Decimal("1.10"),   # slight boost (will be capped by max_bet_pct)
    "TRENDING":       Decimal("1.00"),   # full Kelly
    "MEAN_REVERTING": Decimal("0.75"),   # 75% of Kelly
    "UNKNOWN":        Decimal("0.60"),   # unknown regime → cautious
}

# ---------------------------------------------------------------------------
# Global risk budget — portfolio-level hard cap on total deployed capital
# ---------------------------------------------------------------------------
GLOBAL_RISK_BUDGET: dict = {
    # Maximum fraction of equity that may be in open positions simultaneously.
    # e.g. 0.50 = never deploy more than 50% of equity at once, across ALL markets.
    "max_exposure_pct": Decimal("0.50"),

    # Maximum fraction of equity allowed in a *single* market.
    # Prevents concentration risk when one opportunity looks outstanding.
    "max_per_market_pct": Decimal("0.10"),
}
