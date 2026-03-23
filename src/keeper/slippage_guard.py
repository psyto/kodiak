"""
Spot Slippage Guard — checks order book depth before opening DN positions.

Prevents entering delta-neutral positions when spot liquidity is too thin,
which would cause slippage and break the hedge.

Reads the L2 order book for both spot and perp markets and estimates
the slippage cost for a given trade size. Rejects trades where projected
slippage exceeds a configurable threshold.
"""

import requests
from dataclasses import dataclass

from src.config.constants import HL_MAINNET_API


# Spot pair identifiers on HL (use @index format for l2Book)
SPOT_BOOK_NAMES = {
    "HYPE": "@107",
}


@dataclass
class SlippageEstimate:
    coin: str
    side: str               # "buy" or "sell"
    market_type: str         # "spot" or "perp"
    size_coins: float
    estimated_slippage_pct: float
    depth_usd: float         # Total depth on the relevant side
    sufficient: bool         # True if slippage is within threshold
    reason: str


def estimate_slippage(
    coin: str,
    size_coins: float,
    side: str = "buy",
    market_type: str = "spot",
    api_url: str = HL_MAINNET_API,
) -> SlippageEstimate:
    """
    Estimate slippage for a given trade size by walking the order book.

    Args:
        coin: Asset name (e.g., "HYPE")
        size_coins: Trade size in coins
        side: "buy" or "sell"
        market_type: "spot" or "perp"
        api_url: HL API URL

    Returns:
        SlippageEstimate with projected slippage and depth info
    """
    # Determine book identifier
    if market_type == "spot":
        book_name = SPOT_BOOK_NAMES.get(coin)
        if not book_name:
            return SlippageEstimate(
                coin=coin, side=side, market_type=market_type,
                size_coins=size_coins, estimated_slippage_pct=0,
                depth_usd=0, sufficient=False,
                reason=f"No spot book mapping for {coin}",
            )
    else:
        book_name = coin

    try:
        resp = requests.post(f"{api_url}/info", json={
            "type": "l2Book", "coin": book_name
        }, timeout=10)
        data = resp.json()

        if not data or "levels" not in data:
            return SlippageEstimate(
                coin=coin, side=side, market_type=market_type,
                size_coins=size_coins, estimated_slippage_pct=0,
                depth_usd=0, sufficient=False,
                reason=f"No order book data for {book_name}",
            )

        # Buy = take from asks, Sell = take from bids
        levels = data["levels"][1] if side == "buy" else data["levels"][0]
        if not levels:
            return SlippageEstimate(
                coin=coin, side=side, market_type=market_type,
                size_coins=size_coins, estimated_slippage_pct=0,
                depth_usd=0, sufficient=False,
                reason=f"Empty {side} side of order book",
            )

        # Walk the book to estimate fill price
        remaining = size_coins
        total_cost = 0.0
        total_depth_usd = 0.0
        best_price = float(levels[0]["px"])

        for level in levels:
            px = float(level["px"])
            sz = float(level["sz"])
            total_depth_usd += sz * px

            fill_size = min(remaining, sz)
            total_cost += fill_size * px
            remaining -= fill_size

            if remaining <= 0:
                break

        if remaining > 0:
            # Not enough liquidity to fill the order
            return SlippageEstimate(
                coin=coin, side=side, market_type=market_type,
                size_coins=size_coins, estimated_slippage_pct=100,
                depth_usd=total_depth_usd, sufficient=False,
                reason=f"Insufficient depth: need {size_coins:.4f} {coin} but only {size_coins - remaining:.4f} available",
            )

        avg_price = total_cost / size_coins
        slippage_pct = abs(avg_price - best_price) / best_price * 100

        return SlippageEstimate(
            coin=coin, side=side, market_type=market_type,
            size_coins=size_coins, estimated_slippage_pct=slippage_pct,
            depth_usd=total_depth_usd, sufficient=True,
            reason=f"avg_fill=${avg_price:.4f} vs best=${best_price:.4f}",
        )

    except Exception as e:
        return SlippageEstimate(
            coin=coin, side=side, market_type=market_type,
            size_coins=size_coins, estimated_slippage_pct=0,
            depth_usd=0, sufficient=False,
            reason=f"Error: {e}",
        )


def check_dn_slippage(
    coin: str,
    spot_size_coins: float,
    perp_size_coins: float,
    max_slippage_pct: float = 0.5,
    api_url: str = HL_MAINNET_API,
) -> dict:
    """
    Check if both legs of a DN position can be opened with acceptable slippage.

    Args:
        coin: Asset name
        spot_size_coins: Spot buy size
        perp_size_coins: Perp short size
        max_slippage_pct: Maximum acceptable slippage (default 0.5%)
        api_url: HL API URL

    Returns:
        {"ok": bool, "spot": SlippageEstimate, "perp": SlippageEstimate, "reason": str}
    """
    spot_est = estimate_slippage(coin, spot_size_coins, "buy", "spot", api_url)
    perp_est = estimate_slippage(coin, perp_size_coins, "sell", "perp", api_url)

    spot_ok = spot_est.sufficient and spot_est.estimated_slippage_pct <= max_slippage_pct
    perp_ok = perp_est.sufficient and perp_est.estimated_slippage_pct <= max_slippage_pct

    if spot_ok and perp_ok:
        return {
            "ok": True,
            "spot": spot_est,
            "perp": perp_est,
            "reason": f"Slippage OK: spot={spot_est.estimated_slippage_pct:.3f}% perp={perp_est.estimated_slippage_pct:.3f}%",
        }

    reasons = []
    if not spot_ok:
        reasons.append(f"Spot: {spot_est.reason} (slippage={spot_est.estimated_slippage_pct:.3f}%, depth=${spot_est.depth_usd:.0f})")
    if not perp_ok:
        reasons.append(f"Perp: {perp_est.reason} (slippage={perp_est.estimated_slippage_pct:.3f}%, depth=${perp_est.depth_usd:.0f})")

    return {
        "ok": False,
        "spot": spot_est,
        "perp": perp_est,
        "reason": " | ".join(reasons),
    }
