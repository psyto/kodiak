"""
Position Manager — open/close positions on Hyperliquid.
Ported from Yogi's position-manager.ts.

Uses the Hyperliquid Python SDK for order placement.
Supports maker (limit) and taker (market) orders.
Trades on behalf of a vault using the vaultAddress field.
"""

import time
from dataclasses import dataclass

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from src.config.constants import SIZE_DECIMALS
from src.config.vault import STRATEGY_CONFIG
from src.keeper.funding_scanner import FundingRateData


@dataclass
class BasisPosition:
    market: str
    direction: str           # "short" | "long"
    size_usd: float
    size_coin: float
    entry_funding_rate: float
    entry_timestamp: float


def compute_target_allocations(
    total_equity: float,
    ranked_markets: list[FundingRateData],
    current_positions: list[BasisPosition],
) -> dict:
    """
    Compute target allocations across lending and basis trades.
    On Hyperliquid there's no lending, so 100% goes to basis.
    """
    basis_pct = STRATEGY_CONFIG["basis_trade_pct"]
    max_markets = STRATEGY_CONFIG["max_markets_simultaneous"]
    max_per_market = STRATEGY_CONFIG["max_position_pct_per_market"]

    basis_budget = total_equity * basis_pct / 100

    selected = ranked_markets[:max_markets]
    if not selected:
        return {"lending_target": total_equity, "basis_targets": []}

    # Weight allocation by annualized funding rate
    total_score = sum(m.annualized_pct for m in selected)
    if total_score <= 0:
        return {"lending_target": total_equity, "basis_targets": []}

    basis_targets = []
    for market in selected:
        weight = market.annualized_pct / total_score
        raw_allocation = basis_budget * weight
        max_allocation = total_equity * max_per_market / 100
        size_usd = min(raw_allocation, max_allocation)

        basis_targets.append({
            "market": market.market,
            "size_usd": size_usd,
        })

    return {"lending_target": 0, "basis_targets": basis_targets}


def _round_size(coin: str, size: float) -> float:
    """Round size to the appropriate number of decimals for the coin."""
    decimals = SIZE_DECIMALS.get(coin, 2)
    return round(size, decimals)


async def open_basis_position(
    exchange: Exchange,
    info: Info,
    market: str,
    size_usd: float,
    direction: str = "short",
    vault_address: str = None,
) -> dict:
    """
    Open a basis position using limit orders (maker) when possible.

    Args:
        exchange: Hyperliquid Exchange instance
        info: Hyperliquid Info instance
        market: Coin name (e.g., "BTC", "ETH")
        size_usd: Position size in USD
        direction: "short" or "long"
        vault_address: Optional vault address for delegation
    """
    # Get current oracle price
    all_mids = info.all_mids()
    mid_price = float(all_mids.get(market, 0))
    if mid_price <= 0:
        raise ValueError(f"Invalid mid price for {market}: {mid_price}")
    if size_usd <= 0:
        raise ValueError(f"Invalid position size: ${size_usd}")

    # Compute size in coins
    size_coin = _round_size(market, size_usd / mid_price)
    if size_coin <= 0:
        raise ValueError(f"Size too small for {market}: {size_coin}")

    is_buy = direction == "long"

    if STRATEGY_CONFIG["use_limit_orders"]:
        # SHORT: place above mid (willing to sell higher)
        # LONG: place below mid (willing to buy lower)
        spread_sign = -1 if is_buy else 1
        spread_multiplier = 1 + spread_sign * (STRATEGY_CONFIG["limit_order_spread_bps"] / 10000)
        limit_price = round(mid_price * spread_multiplier, 6)

        result = exchange.order(
            coin=market,
            is_buy=is_buy,
            sz=size_coin,
            limit_px=limit_price,
            order_type={"limit": {"tif": "Gtc"}},
            vault_address=vault_address,
        )
        print(
            f"Opened {direction.upper()} LIMIT ${size_usd:.2f} ({size_coin} {market}) "
            f"@ ${limit_price:.2f} (maker) | {result}"
        )
        return result

    # Market order fallback
    result = exchange.market_open(
        coin=market,
        is_buy=is_buy,
        sz=size_coin,
        vault_address=vault_address,
    )
    print(
        f"Opened {direction.upper()} MARKET ${size_usd:.2f} ({size_coin} {market}) "
        f"(taker) | {result}"
    )
    return result


async def close_basis_position(
    exchange: Exchange,
    info: Info,
    market: str,
    vault_address: str = None,
) -> dict:
    """Close an existing position on a market."""
    # Get current position
    user_addr = vault_address or exchange.wallet.address
    user_state = info.user_state(user_addr)

    position = None
    for pos_wrapper in user_state.get("assetPositions", []):
        pos = pos_wrapper.get("position", {})
        if pos.get("coin") == market:
            position = pos
            break

    if position is None:
        print(f"No position to close on {market}")
        return {}

    size = abs(float(position.get("szi", "0")))
    if size == 0:
        print(f"Zero-size position on {market}")
        return {}

    # If currently short (szi < 0), buy to close. If long (szi > 0), sell to close.
    current_szi = float(position.get("szi", "0"))
    is_buy = current_szi < 0  # Short position → buy to close

    if STRATEGY_CONFIG["use_limit_orders"]:
        all_mids = info.all_mids()
        mid_price = float(all_mids.get(market, 0))
        # For closing: place limit slightly better than mid
        spread_sign = -1 if is_buy else 1
        spread_multiplier = 1 + spread_sign * (STRATEGY_CONFIG["limit_order_spread_bps"] / 10000)
        limit_price = round(mid_price * spread_multiplier, 6)

        result = exchange.order(
            coin=market,
            is_buy=is_buy,
            sz=_round_size(market, size),
            limit_px=limit_price,
            order_type={"limit": {"tif": "Gtc"}},
            reduce_only=True,
            vault_address=vault_address,
        )
        print(f"Close LIMIT on {market} ({size} coins) @ ${limit_price:.2f} (maker) | {result}")
        return result

    # Market close
    result = exchange.market_close(
        coin=market,
        vault_address=vault_address,
    )
    print(f"Closed MARKET on {market} (taker) | {result}")
    return result


def should_exit_position(
    position: BasisPosition, current_funding_rate: float
) -> dict:
    """Check if a position should be exited."""
    exit_threshold = STRATEGY_CONFIG["exit_funding_bps"] / 10000

    if current_funding_rate < exit_threshold:
        return {
            "exit": True,
            "reason": f"Funding rate {current_funding_rate * 100:.4f}% below exit threshold",
        }

    return {"exit": False, "reason": ""}
