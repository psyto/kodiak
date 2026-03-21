"""
Cross-Venue Funding Detector — Hyperliquid-specific alpha.

Compares Hyperliquid's predicted funding rate against Binance and Bybit.
When HL funding diverges significantly from CEX funding, it signals
either an arbitrage opportunity or an impending convergence.

Use cases:
1. HL funding >> CEX funding → HL will likely converge down → SHORT is safer
2. HL funding << CEX funding → HL will likely converge up → LONG is safer
3. All venues aligned → strong directional signal, higher confidence

This data source is unique to Hyperliquid's predictedFundings endpoint.
"""

import time
from dataclasses import dataclass

import requests

from src.config.constants import HL_MAINNET_API
from src.config.vault import STRATEGY_CONFIG


@dataclass
class VenueFunding:
    coin: str
    hl_rate: float             # Hyperliquid hourly funding rate
    hl_annualized: float       # Annualized %
    binance_rate: float        # Binance 8h rate (normalized to hourly)
    binance_annualized: float
    bybit_rate: float          # Bybit 8h rate (normalized to hourly)
    bybit_annualized: float
    hl_vs_cex_spread: float    # HL rate - avg CEX rate (hourly)
    spread_annualized: float   # Annualized spread %
    convergence_signal: str    # "hl_high" | "hl_low" | "aligned" | "no_data"
    confidence: float          # 0-100
    # CEX OI data
    binance_oi: float = 0.0    # Binance open interest (USD)
    bybit_oi: float = 0.0      # Bybit open interest (USD)
    oi_signal: str = "no_data" # "oi_surge" | "oi_drop" | "stable" | "no_data"


# Track previous OI for change detection
_previous_oi: dict[str, dict[str, float]] = {}


# Map HL coins to CEX symbols
_COIN_TO_CEX = {
    "BTC": {"binance": "BTCUSDT", "bybit": "BTCUSDT"},
    "ETH": {"binance": "ETHUSDT", "bybit": "ETHUSDT"},
    "SOL": {"binance": "SOLUSDT", "bybit": "SOLUSDT"},
    "HYPE": {"binance": None, "bybit": None},
}


def _normalize_to_hourly(rate: float, interval_hours: int) -> float:
    """Normalize a funding rate to hourly equivalent."""
    if interval_hours <= 0:
        return 0.0
    return rate / interval_hours


def _fetch_binance_oi(coins: list[str]) -> dict[str, float]:
    """Fetch Binance open interest in USD for monitored coins."""
    result = {}
    for coin in coins:
        mapping = _COIN_TO_CEX.get(coin, {})
        symbol = mapping.get("binance") if mapping else None
        if not symbol:
            continue
        try:
            resp = requests.get(
                f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}",
                timeout=5,
            )
            if resp.status_code != 200:
                continue
            oi_data = resp.json()
            # Get price to convert to USD
            price_resp = requests.get(
                f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}",
                timeout=5,
            )
            if price_resp.status_code == 200:
                price = float(price_resp.json().get("price", 0))
                result[coin] = float(oi_data.get("openInterest", 0)) * price
        except Exception:
            pass
    return result


def _fetch_bybit_oi(coins: list[str]) -> dict[str, float]:
    """Fetch Bybit open interest in USD for monitored coins."""
    result = {}
    for coin in coins:
        mapping = _COIN_TO_CEX.get(coin, {})
        symbol = mapping.get("bybit") if mapping else None
        if not symbol:
            continue
        try:
            resp = requests.get(
                f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={symbol}&intervalTime=1h&limit=1",
                timeout=5,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            oi_entry = (data.get("result", {}).get("list") or [{}])[0]
            oi_base = float(oi_entry.get("openInterest", 0))
            # Get price
            ticker_resp = requests.get(
                f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}",
                timeout=5,
            )
            if ticker_resp.status_code == 200:
                ticker_data = ticker_resp.json()
                price = float((ticker_data.get("result", {}).get("list") or [{}])[0].get("lastPrice", 0))
                result[coin] = oi_base * price
        except Exception:
            pass
    return result


def _classify_oi_signal(coin: str, binance_oi: float, bybit_oi: float) -> str:
    """Classify OI change as surge, drop, or stable."""
    global _previous_oi
    total_oi = binance_oi + bybit_oi

    prev = _previous_oi.get(coin)
    _previous_oi[coin] = {"binance": binance_oi, "bybit": bybit_oi}

    if not prev or total_oi == 0:
        return "no_data"

    prev_total = prev.get("binance", 0) + prev.get("bybit", 0)
    if prev_total == 0:
        return "no_data"

    change_pct = ((total_oi - prev_total) / prev_total) * 100

    if change_pct > 10:
        return "oi_surge"
    if change_pct < -10:
        return "oi_drop"
    return "stable"


def fetch_cross_venue_funding(api_url: str = HL_MAINNET_API) -> list[VenueFunding]:
    """
    Fetch predicted funding rates across Hyperliquid, Binance, and Bybit.

    Returns comparison data for each monitored market.
    """
    resp = requests.post(f"{api_url}/info", json={"type": "predictedFundings"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    allowed = STRATEGY_CONFIG["allowed_markets"]
    results = []

    # Fetch CEX OI data
    binance_oi_map = _fetch_binance_oi(allowed)
    bybit_oi_map = _fetch_bybit_oi(allowed)

    for entry in data:
        if not isinstance(entry, list) or len(entry) < 2:
            continue

        coin = entry[0]
        if allowed and coin not in allowed:
            continue

        venues = entry[1]
        hl_rate = 0.0
        binance_rate = 0.0
        bybit_rate = 0.0
        has_hl = False
        has_cex = False

        for venue in venues:
            if not isinstance(venue, list) or len(venue) < 2:
                continue
            name = venue[0]
            info = venue[1]
            if info is None:
                continue

            rate = float(info["fundingRate"])
            interval = int(info.get("fundingIntervalHours", 8))
            hourly = _normalize_to_hourly(rate, interval)

            if name == "HlPerp":
                hl_rate = hourly
                has_hl = True
            elif name == "BinPerp":
                binance_rate = hourly
                has_cex = True
            elif name == "BybitPerp":
                bybit_rate = hourly
                has_cex = True

        if not has_hl:
            continue

        # Compute CEX average (only from available venues)
        cex_rates = []
        if binance_rate != 0:
            cex_rates.append(binance_rate)
        if bybit_rate != 0:
            cex_rates.append(bybit_rate)
        avg_cex = sum(cex_rates) / len(cex_rates) if cex_rates else 0.0

        # Spread: HL - CEX average
        spread = hl_rate - avg_cex

        # Annualize
        hours_per_year = 24 * 365
        hl_annualized = hl_rate * hours_per_year * 100
        binance_annualized = binance_rate * hours_per_year * 100
        bybit_annualized = bybit_rate * hours_per_year * 100
        spread_annualized = spread * hours_per_year * 100

        # Classify convergence signal
        # Threshold: 5% APY spread is significant
        spread_threshold = STRATEGY_CONFIG.get("cross_venue_spread_threshold_apy", 5.0)

        if not has_cex:
            signal = "no_data"
            confidence = 0.0
        elif spread_annualized > spread_threshold:
            signal = "hl_high"  # HL funding higher than CEX → will likely converge down
            confidence = min(100, abs(spread_annualized) / spread_threshold * 50)
        elif spread_annualized < -spread_threshold:
            signal = "hl_low"   # HL funding lower than CEX → will likely converge up
            confidence = min(100, abs(spread_annualized) / spread_threshold * 50)
        else:
            signal = "aligned"  # HL and CEX in agreement → strong directional signal
            # Higher confidence when all venues agree on direction
            if hl_rate > 0 and avg_cex > 0:
                confidence = min(100, (hl_annualized + sum(r * hours_per_year * 100 for r in cex_rates)) / 3)
            elif hl_rate < 0 and avg_cex < 0:
                confidence = min(100, abs(hl_annualized + sum(r * hours_per_year * 100 for r in cex_rates)) / 3)
            else:
                confidence = 20.0  # Mixed signals

        # CEX OI
        b_oi = binance_oi_map.get(coin, 0.0)
        by_oi = bybit_oi_map.get(coin, 0.0)
        oi_sig = _classify_oi_signal(coin, b_oi, by_oi)

        results.append(VenueFunding(
            coin=coin,
            hl_rate=hl_rate,
            hl_annualized=hl_annualized,
            binance_rate=binance_rate,
            binance_annualized=binance_annualized,
            bybit_rate=bybit_rate,
            bybit_annualized=bybit_annualized,
            hl_vs_cex_spread=spread,
            spread_annualized=spread_annualized,
            convergence_signal=signal,
            confidence=confidence,
            binance_oi=b_oi,
            bybit_oi=by_oi,
            oi_signal=oi_sig,
        ))

    return results


def get_cross_venue_adjustment(venue_funding: VenueFunding) -> dict:
    """
    Get trade direction adjustment based on cross-venue funding comparison.

    Returns:
        {
            "adjustment": float (-1 to +1, positive = favor short),
            "reason": str,
        }
    """
    if venue_funding.convergence_signal == "no_data":
        return {"adjustment": 0.0, "reason": "No CEX data available"}

    if venue_funding.convergence_signal == "aligned":
        # All venues agree — strengthen the base signal
        if venue_funding.hl_rate > 0:
            return {
                "adjustment": 0.2,
                "reason": f"All venues positive funding ({venue_funding.spread_annualized:+.1f}% spread) → strengthen SHORT",
            }
        elif venue_funding.hl_rate < 0:
            return {
                "adjustment": -0.2,
                "reason": f"All venues negative funding ({venue_funding.spread_annualized:+.1f}% spread) → strengthen LONG",
            }
        return {"adjustment": 0.0, "reason": "Venues aligned near zero"}

    if venue_funding.convergence_signal == "hl_high":
        # HL funding is higher than CEX → HL will converge down
        # This means SHORT on HL collects more funding now, but rate may drop
        # Still favor SHORT but with awareness of convergence risk
        return {
            "adjustment": 0.1,
            "reason": f"HL funding {venue_funding.spread_annualized:+.1f}% above CEX → SHORT profitable but convergence risk",
        }

    if venue_funding.convergence_signal == "hl_low":
        # HL funding is lower than CEX → HL will converge up
        # If HL is negative while CEX is positive, HL will likely flip positive
        # This is a LONG opportunity (before funding flips)
        return {
            "adjustment": -0.15,
            "reason": f"HL funding {venue_funding.spread_annualized:+.1f}% below CEX → potential LONG as HL converges up",
        }

    return {"adjustment": 0.0, "reason": "Unknown signal"}


def format_cross_venue(venues: list[VenueFunding]) -> str:
    """Format cross-venue funding comparison for logging."""
    if not venues:
        return "Cross-venue: no data"

    lines = ["Cross-venue funding comparison:"]
    for v in venues:
        hl_str = f"HL={v.hl_annualized:+.1f}%"
        bin_str = f"Bin={v.binance_annualized:+.1f}%" if v.binance_rate else "Bin=N/A"
        byb_str = f"Byb={v.bybit_annualized:+.1f}%" if v.bybit_rate else "Byb=N/A"
        spread_str = f"spread={v.spread_annualized:+.1f}%"
        oi_total = v.binance_oi + v.bybit_oi
        oi_str = f" | OI: ${oi_total / 1e6:.1f}M" if oi_total > 0 else ""
        oi_flag = f" [{v.oi_signal}]" if v.oi_signal not in ("stable", "no_data") else ""
        lines.append(
            f"  {v.coin}: {hl_str} | {bin_str} | {byb_str} | {spread_str} → {v.convergence_signal} ({v.confidence:.0f}%){oi_str}{oi_flag}"
        )
    return "\n".join(lines)
