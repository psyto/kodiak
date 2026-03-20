"""
Kodiak strategy configuration.
Ported from Yogi's STRATEGY_CONFIG, tuned for Hyperliquid dynamics.
"""

STRATEGY_CONFIG = {
    # Capital allocation (base — regime engine may override deploymentPct)
    "lending_floor_pct": 0,        # No lending on HL — 100% to perps
    "basis_trade_pct": 100,

    # Funding rate thresholds
    "min_annualized_funding_bps": 500,   # 5% minimum
    "exit_funding_bps": -50,             # -0.5% exit

    # AMM imbalance signals
    "min_signal_strength": 20,
    "use_imbalance_signals": True,

    # Order execution
    # HL base tier: Taker 4.5bps, Maker 1.5bps (no rebate at base tier)
    "use_limit_orders": True,
    "hl_maker_fee_bps": 1.5,
    "hl_taker_fee_bps": 4.5,
    "limit_order_spread_bps": 3,       # Slightly wider than Drift due to higher fees
    "limit_order_timeout_ms": 60_000,
    "estimated_slippage_bps": 1,

    # Low-turnover model
    "min_holding_period_hours": 168,    # 7 days
    "min_funding_advantage_to_rotate_bps": 200,
    "max_rotations_per_week": 2,

    # Market quality filters
    "max_markets_simultaneous": 3,
    "min_market_oi": 5_000_000,         # $5M minimum OI
    "allowed_markets": ["BTC", "ETH", "SOL", "HYPE"],
    "exclude_markets": [],

    # Dynamic leverage control
    # More conservative than Drift — HL allows up to 50x, which means
    # more aggressive liquidation cascades in the market
    "leverage_by_vol_regime": {
        "veryLow": 2.0,
        "low": 1.5,
        "normal": 1.0,
        "high": 0.5,
        "extreme": 0.0,
    },
    "max_leverage": 2,

    "vol_regime_thresholds": {
        "veryLow": 2000,    # <20% annualized
        "low": 3500,        # <35%
        "normal": 5000,     # <50%
        "high": 7500,       # <75%
    },

    # Risk limits
    "max_drawdown_pct": 3,
    "severe_drawdown_pct": 5,
    "max_position_pct_per_market": 40,

    # Health ratio monitoring
    # HL maintenance margin = 50% of initial at max leverage
    "min_margin_ratio": 1.15,
    "critical_margin_ratio": 1.08,
    "health_check_interval_ms": 30 * 1000,

    # Timing
    "rebalance_interval_ms": 4 * 60 * 60 * 1000,    # 4 hours
    "funding_scan_interval_ms": 30 * 60 * 1000,      # 30 min
    "emergency_check_interval_ms": 30 * 1000,         # 30s

    # Signal detection
    "signal_detection_interval_ms": 5 * 60 * 1000,   # 5 min
    "monitored_markets": ["BTC", "ETH", "SOL"],
    "signal_history_size": 12,          # Rolling snapshots (12 × 5min = 1 hour)
    "funding_history_size": 168,        # 7 days of hourly funding samples
    "funding_vol_window": 24,           # Recent entries for funding vol calc

    # Signal thresholds — tuned for HL dynamics
    # HL has more aggressive liquidations, so thresholds are slightly lower
    "signal_thresholds": {
        "oi_shift":    {"low": 4, "high": 12, "critical": 25},
        "oi_drop":     {"low": 4, "high": 12, "critical": 25},
        "funding_vol": {"low": 500, "high": 1500, "critical": 3000},
        "spread":      {"low": 0.3, "high": 1.0, "critical": 2.5},
    },

    # Imbalance signal scoring weights
    "signal_weights": {
        "funding": 0.5,
        "premium": 0.3,
        "oi": 0.2,
    },

    # Signal scoring scale factors
    "signal_scale_factors": {
        "funding": 500,
        "premium": 10,
        "oi": 10,
    },

    # Regime deployment matrix: vol_regime × signal_severity → % capital deployed
    # [NONE, LOW, HIGH, CRITICAL]
    "deployment_matrix": {
        "veryLow": [100, 80, 50, 25],
        "low":     [85,  70, 40, 20],
        "normal":  [70,  55, 30, 15],
        "high":    [50,  35, 20, 10],
        "extreme": [0,   0,  0,  0],
    },

    # Regime leverage matrix: vol_regime × signal_severity → max leverage
    "leverage_matrix": {
        "veryLow": [2.0, 1.5, 1.0, 0.5],
        "low":     [1.5, 1.2, 0.8, 0.3],
        "normal":  [1.0, 0.8, 0.5, 0.2],
        "high":    [0.5, 0.3, 0.2, 0.0],
        "extreme": [0.0, 0.0, 0.0, 0.0],
    },

    # Cautious mode signal strength minimum
    "cautious_min_signal_strength": 40,

    # Emergency rebalance trigger
    "emergency_deployment_drop_pct": 30,
}
