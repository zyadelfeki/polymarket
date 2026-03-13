from decimal import Decimal

# ---------------------------------------------------------------------------
# ⚠️  ACTION REQUIRED BEFORE ANY LIVE RUN
# STARTING_CAPITAL must equal your actual deployed bankroll.
# Current value ($13.98) is a stale residual balance captured 2026-03-06 and
# is NOT a valid capital figure.  Using it will cause Kelly and the global
# risk budget (max_exposure_pct=0.50) to compute sizes 100x too small vs a
# $1,000+ bankroll.  Update this to your real balance before starting.
# ---------------------------------------------------------------------------
STARTING_CAPITAL = Decimal("17.95")  # TODO: replace with real deployed capital

# ---------------------------------------------------------------------------
# Charlie signal gate configuration
# These are the hard minimum thresholds that must be met before any order
# is placed.  Raise them to bet less often / with higher conviction only.
# ---------------------------------------------------------------------------
CHARLIE_CONFIG = {
    # Minimum edge (p_win - implied_prob) required for a YES or NO bet.
    # Raised 0.05 → 0.10 (2026-03-04): calibration showed 90% of trades land
    # in the [0.4–0.5) p_win bucket (coin-flip territory).  Doubling the edge
    # floor cuts that bucket almost entirely while preserving the [0.6–0.7)
    # bucket that carries the real 64% win-rate edge.
    "min_edge": Decimal("0.07"),
    # Lowered 0.10 -> 0.07 (2026-03-13): best observed edge in 90+ min paper run was 7.93%.
    # The 0.10 gate was raised for calibration (2026-03-04) but is now blocking ALL trades
    # on a $17.95 paper bankroll. 0.07 preserves the [0.6-0.7) p_win bucket filter
    # while allowing the occasional 7-10% edge trade through for paper validation.

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
    # NOTE: max_daily_loss is an absolute floor, not pct-based.
    # Review and update this when deployed capital exceeds $100.
    "max_daily_loss": Decimal("2.00"),
    # FIXED 2026-03-12: was 25.0 — now matches PERFORMANCE_TRACKER_CONFIG.max_drawdown_halt (15%).
    # The old 25% allowed the bot to blow through the soft 15% halt and keep
    # losing until the circuit breaker finally fired at 25%.  Both stops must agree.
    "max_drawdown_pct": Decimal("15.0"),
    # FIXED 2026-03-12: was 5 — lowered to 4 for tighter protection on micro-bankroll.
    "max_consecutive_losses": 4,
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

# ---------------------------------------------------------------------------
# OFI graduation tracking
# OFI_POLICY_ACTIVE = False — OFI features are computed and logged every order
# but never used as a gate.  Graduation requires:
#   1. Collect ≥30 days of OFI observations alongside trade outcomes.
#   2. Run offline Sharpe comparison: with vs without OFI filter.
#   3. If OFI Sharpe improvement ≥0.1 and p-value <0.05, flip to True.
# Milestone owner: assign before 2026-04-01 or remove the dead code path.
# ---------------------------------------------------------------------------
OFI_POLICY_ACTIVE = False  # keep False until offline Sharpe test is complete

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
    '1450993',  #  7 trades, 14% win, -$919   (added 2026-03-02) NO bets near 50/50, resolved YES
    '1451181',  #  7 trades, 14% win, -$906   (added 2026-03-02) NO bets near 50/50, resolved YES
    # --- added 2026-03-04: calibration audit identified these as chronic losers ---
    '1450921',  #  8 trades, 12% win, -$889
    '1411624',  #  5 trades, 20% win, -$870
    '1437414',  #  6 trades, 17% win, -$686
    '1445053',  #  6 trades, 17% win, -$597
    # --- added 2026-03-06: 0% and 25% win-rate chronic losers ---
    '1487045',  #  5 trades,  0% win, -$468  (all 23:05, multi-bet single window)
    '1487027',  #  8 trades, 25% win, -$375  (all 22:49, NO bets at 0.70-0.73 = negative edge)
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
    # Lowered 0.50 -> 0.25 (2026-03-13): Kelly at $17.95 capital / 7% edge / 0.25 fraction = $0.31.
    # $0.25 floor allows these micro-positions through for paper validation only.
    "min_tradeable_usdc": Decimal("0.25"),
}

# ---------------------------------------------------------------------------
# ML / meta-gate configuration
# ---------------------------------------------------------------------------
# Minimum predicted P(take) required for the meta-gate to approve a trade.
# Raised 0.50 → 0.60 (2026-03-04): only the [0.6–0.7) p_win bucket shows
# real edge (64% actual win rate, 14 samples).  The 0.50 default was
# letting the dense 0.483-win-rate coin-flip bucket slip through.
META_GATE_THRESHOLD: float = 0.60

# ---------------------------------------------------------------------------
# OFI policy graduation flag
# ---------------------------------------------------------------------------
# Set to True ONLY after offline Sharpe validation confirms OFI signal quality.
# When False (default), OFI actions are computed and logged but never gate orders.
# When True, _ofi_action == "WAIT" will defer order submission for that cycle.
OFI_POLICY_ACTIVE: bool = False
