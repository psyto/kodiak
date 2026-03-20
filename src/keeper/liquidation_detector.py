"""
Liquidation Detector — Hyperliquid-specific real liquidation tracking.

Instead of proxying liquidation cascades from OI drop (like Yogi does on Drift),
Kodiak detects actual liquidation events from Hyperliquid's trade data.

On Hyperliquid, liquidation trades have a zero-hash:
  hash = "0x0000000000000000000000000000000000000000000000000000000000000000"

This gives us:
1. Real-time liquidation volume per market
2. Liquidation direction (which side is getting liquidated)
3. Liquidation intensity (volume / time window)
4. Cascade detection (accelerating liquidation rate)

This is a 5th signal dimension that replaces the OI-drop proxy
in the signal detector.
"""

import time
from dataclasses import dataclass, field
from collections import deque

import requests

from src.config.constants import HL_MAINNET_API

ZERO_HASH = "0x0000000000000000000000000000000000000000000000000000000000000000"


@dataclass
class LiquidationEvent:
    coin: str
    side: str       # "A" (sell/ask) or "B" (buy/bid)
    size: float     # In coins
    price: float
    notional: float # USD value
    timestamp: int  # ms


@dataclass
class LiquidationState:
    coin: str
    window_seconds: int
    total_volume_usd: float
    long_liquidations_usd: float    # Longs getting liquidated (sell-side)
    short_liquidations_usd: float   # Shorts getting liquidated (buy-side)
    event_count: int
    intensity: float                # Volume per minute (USD)
    direction_bias: str             # "long_squeezed" | "short_squeezed" | "balanced"
    is_cascade: bool                # Accelerating liquidation rate
    severity: int                   # 0-3 signal severity


# Rolling liquidation history per coin
_liq_history: dict[str, deque] = {}

# Previous intensity for cascade detection
_prev_intensity: dict[str, float] = {}


def fetch_recent_liquidations(
    coin: str, api_url: str = HL_MAINNET_API
) -> list[LiquidationEvent]:
    """
    Fetch recent trades and filter for liquidations (zero-hash trades).
    """
    resp = requests.post(f"{api_url}/info", json={
        "type": "recentTrades",
        "coin": coin,
    }, timeout=10)
    if resp.status_code != 200:
        return []

    trades = resp.json()
    liquidations = []

    for t in trades:
        if t.get("hash") == ZERO_HASH:
            price = float(t["px"])
            size = float(t["sz"])
            liquidations.append(LiquidationEvent(
                coin=coin,
                side=t["side"],
                size=size,
                price=price,
                notional=size * price,
                timestamp=t["time"],
            ))

    return liquidations


def update_liquidation_state(
    coin: str,
    window_seconds: int = 300,
    api_url: str = HL_MAINNET_API,
) -> LiquidationState:
    """
    Fetch recent liquidations and compute state for a coin.

    Args:
        coin: Market to check
        window_seconds: Rolling window for intensity calculation (default 5 min)
        api_url: Hyperliquid API URL
    """
    global _liq_history, _prev_intensity

    # Fetch new liquidations
    new_liqs = fetch_recent_liquidations(coin, api_url)

    # Add to rolling history
    if coin not in _liq_history:
        _liq_history[coin] = deque(maxlen=1000)

    for liq in new_liqs:
        # Deduplicate by timestamp + size (since recentTrades may overlap)
        existing = any(
            l.timestamp == liq.timestamp and l.size == liq.size
            for l in _liq_history[coin]
        )
        if not existing:
            _liq_history[coin].append(liq)

    # Filter to window
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - (window_seconds * 1000)
    window_liqs = [l for l in _liq_history[coin] if l.timestamp >= cutoff_ms]

    # Compute metrics
    total_volume = sum(l.notional for l in window_liqs)
    # "A" (ask/sell) = someone is selling = long getting liquidated
    # "B" (bid/buy) = someone is buying = short getting liquidated
    long_liqs = sum(l.notional for l in window_liqs if l.side == "A")
    short_liqs = sum(l.notional for l in window_liqs if l.side == "B")
    event_count = len(window_liqs)

    # Intensity: USD per minute
    window_minutes = window_seconds / 60
    intensity = total_volume / window_minutes if window_minutes > 0 else 0

    # Direction bias
    if total_volume == 0:
        direction_bias = "balanced"
    elif long_liqs > short_liqs * 2:
        direction_bias = "long_squeezed"
    elif short_liqs > long_liqs * 2:
        direction_bias = "short_squeezed"
    else:
        direction_bias = "balanced"

    # Cascade detection: is intensity accelerating?
    # Require sustained high intensity AND acceleration (not just a single spike)
    prev = _prev_intensity.get(coin, intensity)  # Default to current (no spike on first run)
    is_cascade = intensity > prev * 2.0 and intensity > 5000  # >100% increase and >$5k/min
    _prev_intensity[coin] = intensity

    # Severity classification
    severity = _classify_liq_severity(intensity, event_count, coin)

    return LiquidationState(
        coin=coin,
        window_seconds=window_seconds,
        total_volume_usd=total_volume,
        long_liquidations_usd=long_liqs,
        short_liquidations_usd=short_liqs,
        event_count=event_count,
        intensity=intensity,
        direction_bias=direction_bias,
        is_cascade=is_cascade,
        severity=severity,
    )


def _classify_liq_severity(intensity: float, event_count: int, coin: str) -> int:
    """
    Classify liquidation severity based on intensity.

    Thresholds are per-market because BTC has higher notional than HYPE.
    Using USD intensity (per minute) as the primary metric.
    """
    # Base thresholds (USD/min of liquidation volume)
    thresholds = {
        "BTC":  {"low": 5000, "high": 50000, "critical": 200000},
        "ETH":  {"low": 3000, "high": 30000, "critical": 150000},
        "SOL":  {"low": 1000, "high": 10000, "critical": 50000},
        "HYPE": {"low": 2000, "high": 20000, "critical": 100000},
    }
    t = thresholds.get(coin, {"low": 2000, "high": 20000, "critical": 100000})

    if intensity >= t["critical"]:
        return 3  # CRITICAL
    if intensity >= t["high"]:
        return 2  # HIGH
    if intensity >= t["low"]:
        return 1  # LOW
    return 0  # NONE


def detect_liquidations(
    markets: list[str] = None,
    window_seconds: int = 300,
    api_url: str = HL_MAINNET_API,
) -> dict:
    """
    Run liquidation detection across monitored markets.

    Returns:
        {
            "max_severity": int (0-3),
            "states": list[LiquidationState],
            "cascade_detected": bool,
            "total_volume_usd": float,
        }
    """
    if markets is None:
        markets = ["BTC", "ETH", "SOL", "HYPE"]

    states = []
    max_severity = 0
    cascade_detected = False
    total_volume = 0.0

    for coin in markets:
        try:
            state = update_liquidation_state(coin, window_seconds, api_url)
            states.append(state)
            max_severity = max(max_severity, state.severity)
            if state.is_cascade:
                cascade_detected = True
            total_volume += state.total_volume_usd
        except Exception as e:
            print(f"Liquidation detection error for {coin}: {e}")

    return {
        "max_severity": max_severity,
        "states": states,
        "cascade_detected": cascade_detected,
        "total_volume_usd": total_volume,
    }


def format_liquidation_state(result: dict) -> str:
    """Format liquidation detection results for logging."""
    severity_labels = ["CLEAR", "LOW", "HIGH", "CRITICAL"]
    states = result["states"]

    if not states or result["total_volume_usd"] == 0:
        return "Liquidations: none detected"

    lines = [
        f"Liquidations: {severity_labels[result['max_severity']]} "
        f"(${result['total_volume_usd']:,.0f} total, "
        f"{'CASCADE DETECTED' if result['cascade_detected'] else 'no cascade'})"
    ]

    for s in states:
        if s.total_volume_usd > 0:
            lines.append(
                f"  {s.coin}: ${s.total_volume_usd:,.0f} in {s.window_seconds}s "
                f"({s.event_count} events, ${s.intensity:,.0f}/min) "
                f"[{s.direction_bias}] "
                f"{'CASCADE' if s.is_cascade else ''}"
            )

    return "\n".join(lines)
