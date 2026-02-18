from decimal import Decimal

STARTING_CAPITAL = Decimal("13.98")

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
