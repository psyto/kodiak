"""
Leverage Controller — vol regime classification and target leverage.
Ported from Yogi's leverage-controller.ts.

Uses Parkinson estimator on hourly candles for realized vol.
Fetches candle data from Hyperliquid's candleSnapshot endpoint.
"""

import math
from dataclasses import dataclass

import requests

from src.config.constants import HL_MAINNET_API
from src.config.vault import STRATEGY_CONFIG

VolRegime = str


@dataclass
class LeverageState:
    current_vol: float       # Annualized realized vol (decimal)
    current_vol_bps: int
    regime: VolRegime
    target_leverage: float
    reason: str


def classify_vol_regime(vol_bps: int) -> VolRegime:
    """Classify vol regime from realized volatility in bps."""
    t = STRATEGY_CONFIG["vol_regime_thresholds"]
    if vol_bps < t["veryLow"]:
        return "veryLow"
    if vol_bps < t["low"]:
        return "low"
    if vol_bps < t["normal"]:
        return "normal"
    if vol_bps < t["high"]:
        return "high"
    return "extreme"


def compute_target_leverage(vol_bps: int) -> LeverageState:
    """Compute target leverage based on current market volatility."""
    regime = classify_vol_regime(vol_bps)
    leverage_map = STRATEGY_CONFIG["leverage_by_vol_regime"]
    target_leverage = min(
        leverage_map.get(regime, 0),
        STRATEGY_CONFIG["max_leverage"],
    )

    reason = (
        "Extreme vol — all positions closed"
        if regime == "extreme"
        else f"{regime} vol regime → {target_leverage}x leverage"
    )

    return LeverageState(
        current_vol=vol_bps / 10000,
        current_vol_bps=vol_bps,
        regime=regime,
        target_leverage=target_leverage,
        reason=reason,
    )


def fetch_reference_vol(api_url: str = HL_MAINNET_API) -> int:
    """
    Fetch recent realized volatility for BTC (reference market).
    Uses Parkinson estimator on hourly candles.

    Hyperliquid candleSnapshot endpoint:
    POST /info with {"type": "candleSnapshot", "coin": "BTC", "interval": "1h", "startTime": ..., "endTime": ...}
    """
    import time

    end_time = int(time.time() * 1000)
    # 168 hours = 7 days
    start_time = end_time - (168 * 60 * 60 * 1000)

    # HL API requires "req" wrapper for candleSnapshot
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": "1h",
            "startTime": start_time,
            "endTime": end_time,
        },
    }

    resp = requests.post(f"{api_url}/info", json=payload, timeout=10)
    resp.raise_for_status()
    candles = resp.json()

    if not candles or len(candles) < 10:
        return 3000  # Default to 30% if insufficient data

    # Parkinson estimator
    ln2x4 = 4 * math.log(2)
    sum_log_hl2 = 0.0
    valid_count = 0

    for c in candles:
        high = float(c["h"])
        low = float(c["l"])
        if high <= 0 or low <= 0 or high < low:
            continue
        log_hl = math.log(high / low)
        sum_log_hl2 += log_hl * log_hl
        valid_count += 1

    if valid_count == 0:
        return 3000

    variance = sum_log_hl2 / (ln2x4 * valid_count)
    hours_per_year = 365.25 * 24
    annualized_vol = math.sqrt(variance * hours_per_year)

    result = round(annualized_vol * 10000)  # Return in bps
    if not math.isfinite(result):
        return 3000
    return result
