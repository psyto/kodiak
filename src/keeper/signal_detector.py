"""
Signal Detector — Kodiak's intelligence layer.
Ported from Yogi's drift-signal-detector.ts.

Monitors four anomaly dimensions on Hyperliquid:
1. OI Imbalance Shift — rapid change in long/short ratio
2. Liquidation Cascade — spike in OI drop signaling forced selling
3. Funding Rate Volatility — unstable funding = regime transition
4. Spread Blow-out — mark/oracle divergence indicating stress

Each dimension produces a severity level (0-3). The max severity across
all dimensions becomes the aggregate signal that drives the regime engine.
"""

import math
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from src.config.constants import HL_MAINNET_API
from src.config.vault import STRATEGY_CONFIG
from src.keeper.regime_engine import SIGNAL_NONE, SIGNAL_LOW, SIGNAL_HIGH, SIGNAL_CRITICAL


@dataclass
class SignalEvent:
    dimension: str  # "oi_shift" | "liquidation_cascade" | "funding_volatility" | "spread_blowout"
    severity: int
    reason: str
    timestamp: float
    metrics: dict


@dataclass
class MarketSnapshot:
    market: str
    open_interest: float     # Total OI in USD
    funding_rate: float      # Current hourly funding rate
    mark_price: float
    oracle_price: float
    spread_pct: float        # (mark - oracle) / oracle * 100
    oi_imbalance_pct: float  # Estimated from funding direction


@dataclass
class SignalState:
    severity: int
    events: list
    timestamp: float
    market_snapshots: list


# Rolling history for change detection
_snapshot_history: list = []

# Funding rate history for volatility calculation
_funding_history: dict = {}


def _classify_severity(value: float, thresholds: dict) -> int:
    """Classify severity from a value against low/high/critical thresholds."""
    if value >= thresholds["critical"]:
        return SIGNAL_CRITICAL
    if value >= thresholds["high"]:
        return SIGNAL_HIGH
    if value >= thresholds["low"]:
        return SIGNAL_LOW
    return SIGNAL_NONE


def fetch_market_snapshots(api_url: str = HL_MAINNET_API) -> list[MarketSnapshot]:
    """
    Fetch current market state from Hyperliquid.
    Uses metaAndAssetCtxs endpoint for all asset context data.
    """
    payload = {"type": "metaAndAssetCtxs"}
    resp = requests.post(f"{api_url}/info", json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    # data is [meta, assetCtxs] where meta has universe and assetCtxs has per-asset data
    meta = data[0]
    asset_ctxs = data[1]
    universe = meta["universe"]

    snapshots = []
    for i, ctx in enumerate(asset_ctxs):
        if i >= len(universe):
            break
        coin = universe[i]["name"]
        mark_price = float(ctx["markPx"])
        oracle_price = float(ctx["oraclePx"])
        open_interest = float(ctx["openInterest"]) * oracle_price  # Convert to USD
        funding_rate = float(ctx["funding"])

        spread_pct = ((mark_price - oracle_price) / oracle_price * 100) if oracle_price > 0 else 0

        # Hyperliquid doesn't expose long/short OI split directly.
        # We estimate imbalance from funding direction: positive funding = longs pay shorts
        # Higher funding = more long-heavy
        oi_imbalance_pct = funding_rate * 10000  # rough proxy

        snapshots.append(MarketSnapshot(
            market=coin,
            open_interest=open_interest,
            funding_rate=funding_rate,
            mark_price=mark_price,
            oracle_price=oracle_price,
            spread_pct=spread_pct,
            oi_imbalance_pct=oi_imbalance_pct,
        ))

    return snapshots


def fetch_funding_history(
    coin: str, start_time: int, end_time: int, api_url: str = HL_MAINNET_API
) -> list[dict]:
    """Fetch historical funding rates for a coin."""
    payload = {
        "type": "fundingHistory",
        "coin": coin,
        "startTime": start_time,
        "endTime": end_time,
    }
    resp = requests.post(f"{api_url}/info", json=payload, timeout=10)
    if resp.status_code != 200:
        return []
    return resp.json()


def _detect_oi_shift(
    current: list[MarketSnapshot], history: list[list[MarketSnapshot]]
) -> Optional[SignalEvent]:
    """Detect OI imbalance shift — how fast the imbalance is changing."""
    if len(history) < 2:
        return None

    oldest = history[0]
    oldest_map = {s.market: s for s in oldest}
    max_shift = 0.0
    worst_market = ""

    for curr in current:
        prev = oldest_map.get(curr.market)
        if prev is None:
            continue
        shift = abs(curr.oi_imbalance_pct - prev.oi_imbalance_pct)
        if shift > max_shift:
            max_shift = shift
            worst_market = curr.market

    severity = _classify_severity(max_shift, STRATEGY_CONFIG["signal_thresholds"]["oi_shift"])
    if severity == SIGNAL_NONE:
        return None

    return SignalEvent(
        dimension="oi_shift",
        severity=severity,
        reason=f"OI imbalance shifted {max_shift:.1f}% on {worst_market} in ~{len(history) * 5}min",
        timestamp=time.time(),
        metrics={"max_shift": max_shift, "market": worst_market},
    )


def _detect_liquidation_cascade(
    current: list[MarketSnapshot], history: list[list[MarketSnapshot]]
) -> Optional[SignalEvent]:
    """Detect liquidation cascade — proxied by sudden OI drop."""
    if len(history) < 2:
        return None

    oldest = history[0]
    oldest_map = {s.market: s for s in oldest}
    max_drop = 0.0
    worst_market = ""

    for curr in current:
        prev = oldest_map.get(curr.market)
        if prev is None or prev.open_interest <= 0:
            continue
        drop_pct = (prev.open_interest - curr.open_interest) / prev.open_interest * 100
        if drop_pct > max_drop:
            max_drop = drop_pct
            worst_market = curr.market

    severity = _classify_severity(max_drop, STRATEGY_CONFIG["signal_thresholds"]["oi_drop"])
    if severity == SIGNAL_NONE:
        return None

    return SignalEvent(
        dimension="liquidation_cascade",
        severity=severity,
        reason=f"OI dropped {max_drop:.1f}% on {worst_market} — likely liquidation cascade",
        timestamp=time.time(),
        metrics={"max_drop": max_drop, "market": worst_market},
    )


def _detect_funding_volatility(
    markets: list[str], api_url: str = HL_MAINNET_API
) -> Optional[SignalEvent]:
    """Detect funding rate volatility across major markets."""
    global _funding_history

    max_funding_vol = 0.0
    worst_market = ""
    max_history = STRATEGY_CONFIG["funding_history_size"]
    vol_window = STRATEGY_CONFIG["funding_vol_window"]

    for market in markets:
        history = _funding_history.get(market, [])
        if not history:
            # Fetch 7 days of hourly funding history
            now_ms = int(time.time() * 1000)
            start_ms = now_ms - (max_history * 60 * 60 * 1000)
            fetched = fetch_funding_history(market, start_ms, now_ms, api_url)
            history = [float(e.get("fundingRate", 0)) for e in fetched]
            _funding_history[market] = history

        if len(history) < 10:
            continue

        recent = history[-vol_window:]
        mean = sum(recent) / len(recent)
        variance = sum((r - mean) ** 2 for r in recent) / len(recent)
        std_dev = math.sqrt(variance)

        # Annualize: std_dev per 1h period × sqrt(24 × 365) and convert to bps
        annualized_vol_bps = std_dev * math.sqrt(24 * 365) * 10000

        if annualized_vol_bps > max_funding_vol:
            max_funding_vol = annualized_vol_bps
            worst_market = market

    severity = _classify_severity(max_funding_vol, STRATEGY_CONFIG["signal_thresholds"]["funding_vol"])
    if severity == SIGNAL_NONE:
        return None

    return SignalEvent(
        dimension="funding_volatility",
        severity=severity,
        reason=f"Funding rate vol {max_funding_vol:.0f} bps (annualized) on {worst_market}",
        timestamp=time.time(),
        metrics={"max_funding_vol": max_funding_vol, "market": worst_market},
    )


def _detect_spread_blowout(current: list[MarketSnapshot]) -> Optional[SignalEvent]:
    """Detect spread blow-out — mark/oracle divergence across markets."""
    max_spread = 0.0
    worst_market = ""

    for snap in current:
        abs_spread = abs(snap.spread_pct)
        if abs_spread > max_spread:
            max_spread = abs_spread
            worst_market = snap.market

    severity = _classify_severity(max_spread, STRATEGY_CONFIG["signal_thresholds"]["spread"])
    if severity == SIGNAL_NONE:
        return None

    return SignalEvent(
        dimension="spread_blowout",
        severity=severity,
        reason=f"Mark/oracle spread {max_spread:.2f}% on {worst_market}",
        timestamp=time.time(),
        metrics={"max_spread": max_spread, "market": worst_market},
    )


def detect_signals(
    monitored_markets: Optional[list[str]] = None,
    api_url: str = HL_MAINNET_API,
) -> SignalState:
    """
    Run all signal detections and return aggregate state.
    Called every 5 minutes by the keeper loop.
    """
    global _snapshot_history, _funding_history

    if monitored_markets is None:
        monitored_markets = STRATEGY_CONFIG["monitored_markets"]

    max_history = STRATEGY_CONFIG["signal_history_size"]
    max_funding_hist = STRATEGY_CONFIG["funding_history_size"]

    snapshots = fetch_market_snapshots(api_url)
    monitored = [s for s in snapshots if s.market in monitored_markets]

    events = []

    oi_shift = _detect_oi_shift(monitored, _snapshot_history)
    if oi_shift:
        events.append(oi_shift)

    liquidation = _detect_liquidation_cascade(monitored, _snapshot_history)
    if liquidation:
        events.append(liquidation)

    funding_vol = _detect_funding_volatility(monitored_markets, api_url)
    if funding_vol:
        events.append(funding_vol)

    spread = _detect_spread_blowout(monitored)
    if spread:
        events.append(spread)

    # Update rolling history
    _snapshot_history.append(monitored)
    if len(_snapshot_history) > max_history:
        _snapshot_history.pop(0)

    # Update funding history with latest rates
    for snap in monitored:
        history = _funding_history.get(snap.market, [])
        history.append(snap.funding_rate)
        if len(history) > max_funding_hist:
            history.pop(0)
        _funding_history[snap.market] = history

    # Aggregate severity = max across all dimensions
    severity = max((e.severity for e in events), default=SIGNAL_NONE)

    return SignalState(
        severity=severity,
        events=events,
        timestamp=time.time(),
        market_snapshots=monitored,
    )


def format_signal_state(state: SignalState) -> str:
    """Format signal state for logging."""
    severity_labels = ["CLEAR", "LOW", "HIGH", "CRITICAL"]
    label = severity_labels[state.severity] if state.severity < len(severity_labels) else "?"

    if not state.events:
        return f"Signal: {label} — no anomalies detected"

    details = "\n".join(
        f"  [{severity_labels[e.severity]}] {e.reason}" for e in state.events
    )
    return f"Signal: {label} ({len(state.events)} anomalies)\n{details}"
