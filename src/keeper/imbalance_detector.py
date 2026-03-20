"""
Imbalance Detector — entry direction scoring for Kodiak.
Ported from Yogi's imbalance-detector.ts.

Capitalizes on three inefficiencies:
1. Funding Rate — direct measure of supply/demand imbalance
2. Mark/Oracle Spread — premium will converge → trade into convergence
3. OI dynamics — positioning ahead of funding changes

On Hyperliquid, we use metaAndAssetCtxs for real-time data.
Unlike Drift, HL doesn't expose long/short OI split directly,
so we estimate OI imbalance from funding direction.
"""

from dataclasses import dataclass
from typing import Optional

import requests

from src.config.constants import HL_MAINNET_API
from src.config.vault import STRATEGY_CONFIG


@dataclass
class MarketImbalance:
    market: str
    oracle_price: float
    mark_price: float
    premium_pct: float          # (mark - oracle) / oracle * 100
    open_interest: float        # Total OI in USD
    oi_imbalance_pct: float     # Estimated from funding direction
    funding_rate: float         # Hourly funding rate
    annualized_funding_pct: float
    signal: str                 # "strong_short" | "moderate_short" | "neutral" | "moderate_long" | "strong_long"
    signal_strength: float      # 0-100


def _compute_signal(
    premium_pct: float, oi_imbalance_pct: float, funding_rate: float
) -> tuple[str, float, str]:
    """
    Compute composite signal from three inputs.
    Returns (signal, signal_strength, direction).
    """
    weights = STRATEGY_CONFIG["signal_weights"]
    scales = STRATEGY_CONFIG["signal_scale_factors"]

    # Score each component (-1 to +1 scale, positive = short signal)
    funding_score = max(-1, min(1, funding_rate * scales["funding"]))
    premium_score = max(-1, min(1, premium_pct * scales["premium"]))
    oi_score = max(-1, min(1, oi_imbalance_pct / scales["oi"]))

    # Weighted composite
    composite = (
        funding_score * weights["funding"]
        + premium_score * weights["premium"]
        + oi_score * weights["oi"]
    )

    signal_strength = abs(composite) * 100

    if composite > 0.6:
        return "strong_short", signal_strength, "short"
    elif composite > 0.2:
        return "moderate_short", signal_strength, "short"
    elif composite < -0.6:
        return "strong_long", signal_strength, "long"
    elif composite < -0.2:
        return "moderate_long", signal_strength, "long"
    else:
        return "neutral", signal_strength, "none"


def fetch_market_imbalances(api_url: str = HL_MAINNET_API) -> list[MarketImbalance]:
    """Fetch current market state and compute imbalance signals."""
    payload = {"type": "metaAndAssetCtxs"}
    resp = requests.post(f"{api_url}/info", json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    meta = data[0]
    asset_ctxs = data[1]
    universe = meta["universe"]

    imbalances = []
    for i, ctx in enumerate(asset_ctxs):
        if i >= len(universe):
            break

        coin = universe[i]["name"]
        mark_price = float(ctx["markPx"])
        oracle_price = float(ctx["oraclePx"])
        open_interest = float(ctx["openInterest"]) * oracle_price
        funding_rate = float(ctx["funding"])

        premium_pct = ((mark_price - oracle_price) / oracle_price * 100) if oracle_price > 0 else 0
        # Annualize hourly funding rate
        annualized_funding_pct = funding_rate * 24 * 365 * 100
        # Estimate OI imbalance from funding direction
        oi_imbalance_pct = funding_rate * 10000

        signal, signal_strength, _ = _compute_signal(premium_pct, oi_imbalance_pct, funding_rate)

        imbalances.append(MarketImbalance(
            market=coin,
            oracle_price=oracle_price,
            mark_price=mark_price,
            premium_pct=premium_pct,
            open_interest=open_interest,
            oi_imbalance_pct=oi_imbalance_pct,
            funding_rate=funding_rate,
            annualized_funding_pct=annualized_funding_pct,
            signal=signal,
            signal_strength=signal_strength,
        ))

    return imbalances


def get_trade_direction(
    imbalance: MarketImbalance,
) -> dict:
    """
    Determine trade direction from imbalance signals.
    Returns {"direction": ..., "reason": ..., "confidence": ...}
    """
    min_strength = STRATEGY_CONFIG["min_signal_strength"]
    if imbalance.signal_strength < min_strength:
        return {
            "direction": "none",
            "reason": f"Signal too weak ({imbalance.signal_strength:.0f}% < {min_strength}%)",
            "confidence": imbalance.signal_strength,
        }

    if imbalance.signal in ("strong_short", "moderate_short"):
        reasons = []
        if imbalance.funding_rate > 0:
            reasons.append(f"funding +{imbalance.funding_rate * 100:.3f}%")
        if imbalance.premium_pct > 0:
            reasons.append(f"premium +{imbalance.premium_pct:.3f}%")
        return {
            "direction": "short",
            "reason": f"SHORT: {', '.join(reasons)}",
            "confidence": imbalance.signal_strength,
        }

    if imbalance.signal in ("strong_long", "moderate_long"):
        reasons = []
        if imbalance.funding_rate < 0:
            reasons.append(f"funding {imbalance.funding_rate * 100:.3f}%")
        if imbalance.premium_pct < 0:
            reasons.append(f"discount {imbalance.premium_pct:.3f}%")
        return {
            "direction": "long",
            "reason": f"LONG: {', '.join(reasons)}",
            "confidence": imbalance.signal_strength,
        }

    return {
        "direction": "none",
        "reason": "Neutral — conflicting signals",
        "confidence": imbalance.signal_strength,
    }


def rank_by_imbalance(imbalances: list[MarketImbalance]) -> list[MarketImbalance]:
    """Rank markets by signal strength for capital allocation."""
    allowed = STRATEGY_CONFIG["allowed_markets"]
    excluded = STRATEGY_CONFIG["exclude_markets"]
    min_oi = STRATEGY_CONFIG["min_market_oi"]

    filtered = []
    for m in imbalances:
        if m.market in excluded:
            continue
        if allowed and m.market not in allowed:
            continue
        if m.open_interest < min_oi:
            continue
        if m.signal == "neutral":
            continue
        filtered.append(m)

    return sorted(filtered, key=lambda m: m.signal_strength, reverse=True)
