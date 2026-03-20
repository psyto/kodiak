"""
Funding Pre-Positioning — Hyperliquid-specific alpha.

Hyperliquid settles funding hourly, on the hour. The predicted funding rate
is known before settlement. This module:

1. Tracks time until next funding settlement
2. Evaluates predicted rate vs current position
3. Pre-positions 5-10 minutes before settlement to capture funding
4. Skips settlement if predicted rate is unfavorable or flipping

This is direct alpha from timing a known event — not prediction.

Funding mechanics:
- Settled every hour on the hour (XX:00:00 UTC)
- Rate computed from 5-second samples averaged over the hour
- Payment = position_size × oracle_price × funding_rate
- Positive rate: longs pay shorts
- Negative rate: shorts pay longs
"""

import math
import time
from dataclasses import dataclass

import requests

from src.config.constants import HL_MAINNET_API
from src.config.vault import STRATEGY_CONFIG


@dataclass
class FundingSettlement:
    coin: str
    predicted_rate: float          # Hourly rate
    predicted_annualized: float    # APY %
    seconds_until_settlement: int
    should_be_positioned: bool     # True if we should hold through settlement
    optimal_action: str            # "hold" | "enter_short" | "enter_long" | "exit" | "wait"
    reason: str
    expected_payment_bps: float    # Expected funding payment in bps on notional


def _next_settlement_ms() -> int:
    """Compute next hourly settlement timestamp in ms."""
    now = time.time()
    # Next hour boundary
    current_hour = math.floor(now / 3600) * 3600
    next_hour = current_hour + 3600
    return int(next_hour * 1000)


def fetch_predicted_rates(api_url: str = HL_MAINNET_API) -> dict[str, float]:
    """Fetch Hyperliquid's predicted funding rates for next settlement."""
    resp = requests.post(f"{api_url}/info", json={"type": "predictedFundings"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    rates = {}
    for entry in data:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        coin = entry[0]
        for venue in entry[1]:
            if isinstance(venue, list) and venue[0] == "HlPerp" and venue[1]:
                rates[coin] = float(venue[1]["fundingRate"])
    return rates


def evaluate_settlement(
    coin: str,
    predicted_rate: float,
    current_direction: str | None = None,
    api_url: str = HL_MAINNET_API,
) -> FundingSettlement:
    """
    Evaluate whether to be positioned for the next funding settlement.

    Args:
        coin: Market name
        predicted_rate: Predicted hourly funding rate
        current_direction: Current position direction ("short", "long", or None)
    """
    now_ms = int(time.time() * 1000)
    next_settlement = _next_settlement_ms()
    seconds_until = max(0, (next_settlement - now_ms) // 1000)

    annualized = predicted_rate * 24 * 365 * 100
    # Expected payment in bps (per hour of holding)
    expected_bps = predicted_rate * 10000

    # Pre-positioning window: 10 minutes before settlement
    preposition_window = STRATEGY_CONFIG.get("preposition_window_seconds", 600)
    # Minimum rate to justify positioning (annualized APY)
    min_rate_apy = STRATEGY_CONFIG.get("preposition_min_rate_apy", 5.0)
    min_rate_hourly = min_rate_apy / (24 * 365 * 100)

    in_window = seconds_until <= preposition_window
    rate_is_positive = predicted_rate > min_rate_hourly
    rate_is_negative = predicted_rate < -min_rate_hourly

    # Determine optimal action
    if not in_window:
        return FundingSettlement(
            coin=coin,
            predicted_rate=predicted_rate,
            predicted_annualized=annualized,
            seconds_until_settlement=seconds_until,
            should_be_positioned=False,
            optimal_action="wait",
            reason=f"{seconds_until // 60}min until settlement — too early to pre-position",
            expected_payment_bps=expected_bps,
        )

    # We're in the pre-positioning window
    if rate_is_positive:
        # Positive rate: shorts collect funding
        if current_direction == "short":
            action = "hold"
            reason = f"Hold SHORT — collecting {annualized:+.1f}% APY funding at settlement"
            should_position = True
        elif current_direction == "long":
            action = "exit"
            reason = f"EXIT LONG — paying {annualized:+.1f}% APY funding at settlement"
            should_position = False
        else:
            action = "enter_short"
            reason = f"Enter SHORT before settlement — {annualized:+.1f}% APY funding in {seconds_until // 60}min"
            should_position = True
    elif rate_is_negative:
        # Negative rate: longs collect funding
        if current_direction == "long":
            action = "hold"
            reason = f"Hold LONG — collecting {abs(annualized):.1f}% APY funding at settlement"
            should_position = True
        elif current_direction == "short":
            action = "exit"
            reason = f"EXIT SHORT — paying {abs(annualized):.1f}% APY funding at settlement"
            should_position = False
        else:
            action = "enter_long"
            reason = f"Enter LONG before settlement — {abs(annualized):.1f}% APY funding in {seconds_until // 60}min"
            should_position = True
    else:
        # Rate too low to justify positioning
        action = "wait"
        reason = f"Rate {annualized:+.1f}% APY too low — skip this settlement"
        should_position = current_direction is not None  # Hold existing if we have one

    return FundingSettlement(
        coin=coin,
        predicted_rate=predicted_rate,
        predicted_annualized=annualized,
        seconds_until_settlement=seconds_until,
        should_be_positioned=should_position,
        optimal_action=action,
        reason=reason,
        expected_payment_bps=expected_bps,
    )


def evaluate_all_settlements(
    markets: list[str] = None,
    current_positions: dict[str, str] = None,
    api_url: str = HL_MAINNET_API,
) -> list[FundingSettlement]:
    """
    Evaluate pre-positioning for all monitored markets.

    Args:
        markets: List of coins to evaluate
        current_positions: {coin: direction} of current positions
    """
    if markets is None:
        markets = STRATEGY_CONFIG["allowed_markets"]
    if current_positions is None:
        current_positions = {}

    predicted = fetch_predicted_rates(api_url)
    settlements = []

    for coin in markets:
        rate = predicted.get(coin, 0)
        direction = current_positions.get(coin)
        settlement = evaluate_settlement(coin, rate, direction, api_url)
        settlements.append(settlement)

    return settlements


def format_settlements(settlements: list[FundingSettlement]) -> str:
    """Format settlement evaluation for logging."""
    if not settlements:
        return "Pre-positioning: no data"

    # All settlements share the same time
    secs = settlements[0].seconds_until_settlement if settlements else 0
    mins = secs // 60

    lines = [f"Pre-positioning ({mins}min to settlement):"]
    for s in settlements:
        if s.optimal_action != "wait" or s.predicted_annualized != 0:
            action_emoji = {
                "hold": ">>",
                "enter_short": "vv",
                "enter_long": "^^",
                "exit": "XX",
                "wait": "..",
            }.get(s.optimal_action, "??")
            lines.append(
                f"  [{action_emoji}] {s.coin}: {s.predicted_annualized:+.1f}% APY "
                f"({s.expected_payment_bps:+.2f} bps) — {s.reason}"
            )

    return "\n".join(lines)
