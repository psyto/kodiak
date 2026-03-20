"""
Health Monitor — margin and drawdown monitoring for Kodiak.
Ported from Yogi's health-monitor.ts.

Uses Hyperliquid's clearinghouseState endpoint to monitor:
- Account value (equity)
- Margin usage
- Unrealized PnL
- Drawdown from peak equity
"""

from dataclasses import dataclass

from src.config.vault import STRATEGY_CONFIG


@dataclass
class HealthState:
    total_equity: float          # USD
    maintenance_margin: float    # USD
    margin_ratio: float          # equity / margin (>1.0 = healthy)
    unrealized_pnl: float       # USD
    status: str                  # "healthy" | "warning" | "critical" | "liquidatable"
    action: str                  # "none" | "reduce" | "close_all"


def compute_health_state(user_state: dict) -> HealthState:
    """
    Compute health state from Hyperliquid clearinghouseState.

    user_state format:
    {
        "marginSummary": {
            "accountValue": "1234.56",
            "totalMarginUsed": "100.00",
            "totalNtlPos": "500.00",
            "totalRawUsd": "1234.56",
        },
        "crossMarginSummary": {
            "accountValue": "...",
            "totalMarginUsed": "...",
            "totalNtlPos": "...",
        },
        "assetPositions": [...],
    }
    """
    margin_summary = user_state.get("marginSummary", {})
    account_value = float(margin_summary.get("accountValue", "0"))
    total_margin_used = float(margin_summary.get("totalMarginUsed", "0"))

    # Compute unrealized PnL from positions
    unrealized_pnl = 0.0
    for pos_wrapper in user_state.get("assetPositions", []):
        pos = pos_wrapper.get("position", {})
        upnl = float(pos.get("unrealizedPnl", "0"))
        unrealized_pnl += upnl

    # Margin ratio: account_value / margin_used
    margin_ratio = (account_value / total_margin_used) if total_margin_used > 0 else float("inf")

    if margin_ratio <= 1.0:
        status = "liquidatable"
        action = "close_all"
    elif margin_ratio <= STRATEGY_CONFIG["critical_margin_ratio"]:
        status = "critical"
        action = "close_all"
    elif margin_ratio <= STRATEGY_CONFIG["min_margin_ratio"]:
        status = "warning"
        action = "reduce"
    else:
        status = "healthy"
        action = "none"

    return HealthState(
        total_equity=account_value,
        maintenance_margin=total_margin_used,
        margin_ratio=margin_ratio,
        unrealized_pnl=unrealized_pnl,
        status=status,
        action=action,
    )


def compute_drawdown(
    current_equity: float, peak_equity: float
) -> dict:
    """Compute drawdown from peak equity."""
    if peak_equity <= 0:
        return {"drawdown_pct": 0, "action": "none"}

    drawdown_pct = (peak_equity - current_equity) / peak_equity * 100

    if drawdown_pct >= STRATEGY_CONFIG["severe_drawdown_pct"]:
        return {"drawdown_pct": drawdown_pct, "action": "close_all"}
    if drawdown_pct >= STRATEGY_CONFIG["max_drawdown_pct"]:
        return {"drawdown_pct": drawdown_pct, "action": "reduce"}
    return {"drawdown_pct": drawdown_pct, "action": "none"}
