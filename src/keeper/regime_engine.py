"""
Regime Engine — Kodiak's decision matrix.
Ported from Yogi's regime-engine.ts.

Combines two independent inputs:
1. Vol Regime (from leverage controller) — backward-looking realized vol
2. Signal Severity (from signal detector) — forward-looking anomaly detection

The combination produces a regime that determines:
- deployment_pct: how much capital to deploy (0-100%)
- max_leverage: maximum allowed leverage
- rebalance_mode: aggressive, normal, cautious, or defensive
"""

from dataclasses import dataclass
from typing import Optional

from src.config.vault import STRATEGY_CONFIG

# Signal severity levels
SIGNAL_NONE = 0
SIGNAL_LOW = 1
SIGNAL_HIGH = 2
SIGNAL_CRITICAL = 3

VolRegime = str  # "veryLow" | "low" | "normal" | "high" | "extreme"
RebalanceMode = str  # "aggressive" | "normal" | "cautious" | "defensive"


@dataclass
class Regime:
    vol_regime: VolRegime
    signal_severity: int
    deployment_pct: float
    max_leverage: float
    rebalance_mode: RebalanceMode
    reason: str


def compute_regime(vol_regime: VolRegime, signal_severity: int) -> Regime:
    """Compute current regime from vol + signals."""
    deploy_row = STRATEGY_CONFIG["deployment_matrix"].get(vol_regime, [0, 0, 0, 0])
    leverage_row = STRATEGY_CONFIG["leverage_matrix"].get(vol_regime, [0, 0, 0, 0])

    deployment_pct = deploy_row[signal_severity] if signal_severity < len(deploy_row) else 0
    max_leverage = leverage_row[signal_severity] if signal_severity < len(leverage_row) else 0

    if deployment_pct >= 85:
        rebalance_mode = "aggressive"
    elif deployment_pct >= 55:
        rebalance_mode = "normal"
    elif deployment_pct >= 20:
        rebalance_mode = "cautious"
    else:
        rebalance_mode = "defensive"

    severity_labels = ["clear", "low", "high", "critical"]
    if signal_severity == SIGNAL_NONE:
        reason = f"{vol_regime} vol, no anomalies -> {deployment_pct}% deployed @ {max_leverage}x"
    else:
        label = severity_labels[signal_severity] if signal_severity < len(severity_labels) else "?"
        reason = (
            f"{vol_regime} vol + {label} signal -> "
            f"{deployment_pct}% deployed @ {max_leverage}x ({rebalance_mode})"
        )

    return Regime(
        vol_regime=vol_regime,
        signal_severity=signal_severity,
        deployment_pct=deployment_pct,
        max_leverage=max_leverage,
        rebalance_mode=rebalance_mode,
        reason=reason,
    )


def should_trigger_emergency_rebalance(
    previous: Optional[Regime], current: Regime
) -> bool:
    """Determine if regime change warrants immediate action."""
    if previous is None:
        return False

    deployment_drop = previous.deployment_pct - current.deployment_pct
    if deployment_drop >= STRATEGY_CONFIG["emergency_deployment_drop_pct"]:
        return True

    if previous.signal_severity <= SIGNAL_LOW and current.signal_severity >= SIGNAL_CRITICAL:
        return True

    if previous.rebalance_mode != "defensive" and current.rebalance_mode == "defensive":
        return True

    return False


def format_regime(regime: Regime) -> str:
    """Format regime for logging."""
    mode_prefix = {
        "aggressive": ">>",
        "normal": "->",
        "cautious": "~~",
        "defensive": "!!",
    }
    return f"[{mode_prefix.get(regime.rebalance_mode, '??')}] {regime.reason}"
