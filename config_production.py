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
# Permanently blocked markets — chronic losers with near-zero win rates.
# Any market whose condition_id OR numeric id appears here is skipped at
# scan time and logged as 'market_blocked'.  Add future losers here.
#
# Combined loss on the 8 markets below: -$4,964 over 110 trades @ <20% win rate.
# Root cause: signal model has NO edge on these specific question types.
# ---------------------------------------------------------------------------
BLOCKED_MARKETS: set = {
    '1402904',  # 15 trades, 0% win,  -$150
    '1402902',  # 15 trades, 0% win,  -$150
    '1403073',  # 52 trades, 2% win,  -$480
    '1403228',  #  5 trades, 0% win,  -$917
    '1403232',  #  5 trades, 20% win, -$884
    '1403143',  #  6 trades, 17% win, -$873
    '1445001',  #  6 trades, 0% win,  -$942.95
    '1447205',  #  7 trades, 14% win, -$958.75
    '1448902',  #  6 trades, 17% win, -$873   (added 2026-02-27)
    '1448693',  #  6 trades, 17% win, -$917   (added 2026-02-27)
}

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
# Regime-based risk config for the Volatility/Regime Classifier (Session 2).
#
# Each regime maps to independent risk parameters that override KELLY_CONFIG
# for the duration of that regime.  The regime is re-evaluated every 60 s by
# the periodic regime-update task in main.py.
#
# Design rationale for defaults:
#   calm       → moderate Kelly, standard min_edge.  Steady conditions.
#   trend_up   → slightly aggressive; our latency-arb edge is strongest here.
#   trend_down → same as trend_up but we are betting NO; same edge logic.
#   event      → half Kelly, raised min_edge.  Volatility spikes kill edge.
#
# These override KELLY_CONFIG["fractional_kelly"] and
# KELLY_CONFIG["min_edge_required"] IN MEMORY only; the config file is never
# rewritten.  Default (fallback) is always KELLY_CONFIG values.
# ---------------------------------------------------------------------------
REGIME_RISK_CONFIG: dict = {
    "calm": {
        "fractional_kelly":  Decimal("0.25"),
        "min_edge_required": Decimal("0.02"),
        "max_bet_pct":       Decimal("5.0"),
    },
    "trend_up": {
        "fractional_kelly":  Decimal("0.30"),
        "min_edge_required": Decimal("0.02"),
        "max_bet_pct":       Decimal("5.0"),
    },
    "trend_down": {
        "fractional_kelly":  Decimal("0.30"),
        "min_edge_required": Decimal("0.02"),
        "max_bet_pct":       Decimal("5.0"),
    },
    "event": {
        "fractional_kelly":  Decimal("0.10"),
        "min_edge_required": Decimal("0.03"),
        "max_bet_pct":       Decimal("3.0"),
    },
}

# ---------------------------------------------------------------------------
# Market tag blocklist (Session 3 — LLM Question Tagger).
#
# Each entry is a dict of tag conditions that must ALL match for a market to
# be blocked.  Any single entry that fully matches will block the market.
#
# Example: block politics / long-term markets and macro_data / multi-day
# markets where our BTC-centric stack has historically no edge.
#
# Tags are produced by scripts/tag_market_questions.py and stored in
# data/market_tags.db.  If tags are unavailable for a market, the
# market is NOT blocked (fail-open).
# ---------------------------------------------------------------------------
MARKET_TAG_BLOCKLIST: list = [
    # Politics + any horizon → no edge with our BTC stack
    {"event_type": "election"},
    {"event_type": "politics"},
    # Long-term binary_misc markets (no directional signal)
    {"horizon": "long-term", "outcome_type": "binary_misc"},
    # High info_edge_needed + long-term → unfavourable
    {"info_edge_needed": "high", "horizon": "long-term"},
    # Non-BTC/ETH assets on long-term horizon
    {"asset": "macro", "horizon": "long-term"},
    {"asset": "other", "horizon": "long-term"},
]

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

    # Absolute minimum USDC size that is worth placing on Polymarket.
    # Below this, a position is too small to be meaningful even if Kelly and
    # all risk gates approve it.  Polymarket practical minimum is ~$1.
    # $0.50 gives some room while never vetoing a legitimately-sized bet
    # at this capital level ($13.98 → 5% cap = $0.70, which passes $0.50).
    "min_tradeable_usdc": Decimal("0.50"),
}

# ---------------------------------------------------------------------------
# ML / meta-gate configuration
# ---------------------------------------------------------------------------
# Minimum predicted P(take) required for the meta-gate to approve a trade.
# 0.50 is the calibrated default for cold-start (< 200 settled trades).
# Raise towards 0.55-0.60 once the model accumulates >500 labelled samples
# and calibration ECE improves.
META_GATE_THRESHOLD: float = 0.50

# ---------------------------------------------------------------------------
# OFI policy graduation flag
# ---------------------------------------------------------------------------
# Set to True ONLY after offline Sharpe validation confirms OFI signal quality.
# When False (default), OFI actions are computed and logged but never gate orders.
# When True, _ofi_action == "WAIT" will defer order submission for that cycle.
OFI_POLICY_ACTIVE: bool = False
