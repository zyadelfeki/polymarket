# ML Model Configuration

ML_ENSEMBLE_CONFIG = {
    'n_models': 5,
    'model_type': 'gradient_boosting',
    'hyperparameters': {
        'n_estimators': 100,
        'learning_rate': 0.05,
        'max_depth': 5,
        'min_samples_split': 20,
        'subsample': 0.8,
    },
    'min_mispricing_edge': 0.03,  # 3%
    'min_confidence': 0.30,
    'calibration_method': 'sigmoid',
}

LATENCY_ARBITRAGE_CONFIG = {
    'enabled': True,
    'min_edge': 0.05,  # 5% difference to trigger trade
    'max_slippage': 0.02,  # 2%
    'latency_window': 60,  # seconds
    'exit_time': 30,  # Exit within 30 seconds
    'target_price_offset': 0.40,  # Try to get to 40 cents if bought at 5 cents
    'stop_loss_pct': 0.05,  # -5% stop loss
}

WHALE_TRACKING_CONFIG = {
    'enabled': True,
    'n_whales_to_track': 50,
    'min_whale_trade_size': 5000,  # Only copy trades >$5K
    'copy_scale': 0.05,  # Copy max 5% of whale size
    'min_whale_edge': 0.08,  # Only copy if estimated edge >8%
    'copy_exit_time': 300,  # 5 minutes max hold
    'update_frequency': 1,  # Check every 1 second
}

LIQUIDITY_SHOCK_CONFIG = {
    'enabled': True,
    'shock_threshold': 0.30,  # 30% drop = shock
    'min_liquidity': 100,  # Minimum liquidity to consider
    'check_frequency': 5,  # Check every 5 seconds
    'shock_exit_time': 300,  # Hold 5 minutes
    'imbalance_ratio': 3.0,  # 3:1 ratio = severe imbalance
}

CROSS_MARKET_ARBITRAGE_CONFIG = {
    'enabled': False,  # Not yet implemented
    'min_price_divergence': 0.02,  # 2% difference
    'platforms': ['polymarket', 'kalshi', 'manifold'],
    'check_frequency': 10,  # Every 10 seconds
}
