"""
Funding Scanner — fetch and rank markets by funding rate on Hyperliquid.
Ported from Yogi's funding-scanner.ts.

Hyperliquid provides:
- Current funding via metaAndAssetCtxs
- Predicted funding via predictedFundings (cross-venue comparison)
- Historical funding via fundingHistory
"""

import time
from dataclasses import dataclass

import requests

from src.config.constants import HL_MAINNET_API
from src.config.vault import STRATEGY_CONFIG
from src.keeper.cost_calculator import passes_cost_gate


@dataclass
class FundingRateData:
    market: str
    rate_hourly: float          # Current hourly funding rate
    annualized_pct: float       # Annualized percentage
    open_interest: float        # USD
    predicted_rate: float       # Predicted next funding rate


def fetch_all_funding_rates(api_url: str = HL_MAINNET_API) -> list[FundingRateData]:
    """Fetch current funding rates for all perp markets."""
    payload = {"type": "metaAndAssetCtxs"}
    resp = requests.post(f"{api_url}/info", json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    meta = data[0]
    asset_ctxs = data[1]
    universe = meta["universe"]

    # Also fetch predicted funding rates for cross-venue comparison
    predicted = {}
    try:
        pred_payload = {"type": "predictedFundings"}
        pred_resp = requests.post(f"{api_url}/info", json=pred_payload, timeout=10)
        if pred_resp.status_code == 200:
            pred_data = pred_resp.json()
            for entry in pred_data:
                if isinstance(entry, list) and len(entry) >= 2:
                    coin = entry[0]
                    venues = entry[1]
                    # Find Hyperliquid's predicted rate
                    for venue in venues:
                        if isinstance(venue, list) and len(venue) >= 2:
                            if venue[0] == "Hyperliquid":
                                predicted[coin] = float(venue[1])
    except Exception:
        pass  # Predicted funding is optional

    rates = []
    for i, ctx in enumerate(asset_ctxs):
        if i >= len(universe):
            break

        coin = universe[i]["name"]
        funding_rate = float(ctx["funding"])
        oracle_price = float(ctx["oraclePx"])
        open_interest = float(ctx["openInterest"]) * oracle_price

        # Annualize: hourly rate × 24 × 365 × 100 for percentage
        annualized_pct = funding_rate * 24 * 365 * 100

        rates.append(FundingRateData(
            market=coin,
            rate_hourly=funding_rate,
            annualized_pct=annualized_pct,
            open_interest=open_interest,
            predicted_rate=predicted.get(coin, 0),
        ))

    return rates


def rank_markets_by_funding(
    rates: list[FundingRateData],
    min_annualized_bps: float = 0,
) -> list[FundingRateData]:
    """Rank markets by funding rate, applying filters."""
    if min_annualized_bps == 0:
        min_annualized_bps = STRATEGY_CONFIG["min_annualized_funding_bps"]

    min_pct = min_annualized_bps / 100
    allowed = STRATEGY_CONFIG["allowed_markets"]
    excluded = STRATEGY_CONFIG["exclude_markets"]

    filtered = []
    for r in rates:
        if r.market in excluded:
            continue
        if allowed and r.market not in allowed:
            continue
        # Require positive funding
        if r.annualized_pct < min_pct:
            continue
        filtered.append(r)

    # Sort by annualized funding rate (highest first)
    return sorted(filtered, key=lambda r: r.annualized_pct, reverse=True)
