"""
Delta-Neutral Position Manager — Kodiak's upgraded execution layer.

Instead of directional perp shorts, this module manages paired positions:
  - Buy spot asset (e.g., HYPE)
  - Short same asset on perps (e.g., HYPE-PERP)

Price movement cancels out. Profit comes purely from funding rate collection.

Architecture:
  70% of capital → spot buy (the hedge)
  30% of capital → perp margin (for the short)
  Effective notional = 70% of capital (matched spot + perp)

Reference: github.com/cgaspart/HL-Delta
"""

import time
from dataclasses import dataclass
from typing import Optional

import requests

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from src.config.constants import SIZE_DECIMALS, HL_MAINNET_API
from src.config.vault import STRATEGY_CONFIG


# Spot/perp allocation ratio
SPOT_RATIO = 0.70   # 70% to spot
MARGIN_RATIO = 0.30  # 30% for perp margin

# Map perp coin names to spot pair names on Hyperliquid
# Perp uses "HYPE" (asset 159), Spot uses "HYPE/USDC" (asset 10107)
# Only HYPE has a liquid spot/USDC pair on Hyperliquid.
# BTC/ETH/SOL don't have direct spot pairs — DN only works for HYPE.
PERP_TO_SPOT = {
    "HYPE": "HYPE/USDC",
}


@dataclass
class DeltaNeutralPosition:
    coin: str
    spot_size: float          # Coins held in spot
    perp_size: float          # Coins shorted in perp
    spot_entry_price: float
    perp_entry_price: float
    entry_funding_rate: float
    entry_timestamp: float
    cumulative_funding: float = 0.0
    tilt_pct: float = 0.0     # Short bias: 0.0 = pure DN, 0.1 = 10% extra short

    @property
    def delta(self) -> float:
        """Net exposure: spot - abs(perp). Negative = short bias."""
        return self.spot_size - abs(self.perp_size)

    @property
    def delta_pct(self) -> float:
        """Delta as percentage of spot size."""
        return (self.delta / self.spot_size * 100) if self.spot_size > 0 else 0

    @property
    def notional_usd(self) -> float:
        """Approximate USD value of one leg."""
        avg_price = (self.spot_entry_price + self.perp_entry_price) / 2
        return self.spot_size * avg_price


def _round_size(coin: str, size: float, info: Info = None) -> float:
    """Round size to appropriate decimals for the coin."""
    if info and hasattr(info, 'coin_to_asset') and hasattr(info, 'asset_to_sz_decimals'):
        asset = info.coin_to_asset.get(coin)
        if asset is not None and asset in info.asset_to_sz_decimals:
            decimals = info.asset_to_sz_decimals[asset]
            return round(size, decimals)
    decimals = SIZE_DECIMALS.get(coin, 2)
    return round(size, decimals)


def _get_spot_meta(api_url: str = HL_MAINNET_API) -> dict:
    """Fetch spot metadata to get token index for spot trading."""
    resp = requests.post(f"{api_url}/info", json={"type": "spotMeta"}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _get_spot_token_index(coin: str, api_url: str = HL_MAINNET_API) -> Optional[int]:
    """
    Get the spot token index for a coin.
    Spot assets use index 10000 + token_index in the SDK.
    """
    try:
        meta = _get_spot_meta(api_url)
        for i, token in enumerate(meta.get("tokens", [])):
            if token.get("name") == coin:
                return token.get("index", i)
        # Also check universe
        for i, entry in enumerate(meta.get("universe", [])):
            tokens = entry.get("tokens", [])
            if any(t == coin for t in [entry.get("name", "")] + [str(t) for t in tokens]):
                return i
    except Exception as e:
        print(f"Error fetching spot token index for {coin}: {e}")
    return None


def get_spot_balance(coin: str, info: Info, user_addr: str, api_url: str = HL_MAINNET_API) -> float:
    """Get spot balance of a coin for the user/vault."""
    try:
        resp = requests.post(f"{api_url}/info", json={
            "type": "spotClearinghouseState", "user": user_addr
        }, timeout=10)
        if resp.status_code != 200:
            return 0.0
        state = resp.json()
        for balance in state.get("balances", []):
            if balance.get("coin") == coin:
                return float(balance.get("total", 0))
    except Exception:
        pass
    return 0.0


async def open_delta_neutral(
    exchange: Exchange,
    info: Info,
    coin: str,
    capital_usd: float,
    vault_address: str = None,
    api_url: str = HL_MAINNET_API,
    tilt_pct: float = None,
) -> Optional[DeltaNeutralPosition]:
    """
    Open a delta-neutral position with optional short tilt.

    Args:
        exchange: Hyperliquid Exchange instance
        info: Hyperliquid Info instance
        coin: Asset name (e.g., "HYPE")
        capital_usd: Total capital to deploy for this position
        vault_address: Optional vault address
        api_url: API URL
        tilt_pct: Short bias (0.0 = pure DN, 0.1 = 10% extra short). Defaults to config.

    Returns:
        DeltaNeutralPosition or None if failed
    """
    if tilt_pct is None:
        tilt_pct = STRATEGY_CONFIG.get("dn_tilt_pct", 0.0)

    # Get current price
    all_mids = info.all_mids()
    mid_price = float(all_mids.get(coin, 0))
    if mid_price <= 0:
        print(f"Invalid mid price for {coin}: {mid_price}")
        return None

    # Calculate sizes
    spot_capital = capital_usd * SPOT_RATIO
    spot_size_coins = _round_size(coin, spot_capital / mid_price, info)

    if spot_size_coins <= 0:
        print(f"Spot size too small for {coin}")
        return None

    # Apply tilt: perp short is larger than spot by tilt_pct
    # e.g., tilt_pct=0.1 → perp is 10% larger than spot → slight short bias
    perp_size_coins = _round_size(coin, spot_size_coins * (1 + tilt_pct), info)

    spot_notional = spot_size_coins * mid_price
    perp_notional = perp_size_coins * mid_price
    tilt_label = f" | tilt={tilt_pct*100:.0f}% short bias" if tilt_pct > 0 else ""

    print(f"\n--- Opening Delta-Neutral: {coin}{tilt_label} ---")
    print(f"Capital: ${capital_usd:.2f} (spot: ${spot_capital:.2f}, margin: ${capital_usd - spot_capital:.2f})")
    print(f"Spot: {spot_size_coins} {coin} | Perp: {perp_size_coins} {coin} @ ${mid_price:.2f}")
    print(f"Spot notional: ${spot_notional:.2f} | Perp notional: ${perp_notional:.2f}")

    # Step 1: Buy spot
    # IMPORTANT: On Hyperliquid, perp uses coin name ("HYPE") but spot uses
    # pair name ("HYPE/USDC"). Using just "HYPE" opens a perp long, not spot.
    spot_symbol = PERP_TO_SPOT.get(coin)
    if not spot_symbol:
        print(f"No spot pair mapping for {coin}")
        return None

    try:
        # Use limit order slightly above mid for immediate fill
        spot_price = round(mid_price * 1.001, 2 if mid_price < 100 else 0)
        spot_result = exchange.order(
            name=spot_symbol,
            is_buy=True,
            sz=spot_size_coins,
            limit_px=spot_price,
            order_type={"limit": {"tif": "Ioc"}},  # Immediate-or-cancel for fast fill
        )
        print(f"Spot BUY: {spot_size_coins} {spot_symbol} @ ${spot_price} | {spot_result}")

        # Check if spot order filled
        status = spot_result.get("response", {}).get("data", {}).get("statuses", [{}])
        if status and "error" in str(status[0]):
            print(f"Spot order failed: {status}")
            return None

    except Exception as e:
        print(f"Spot buy failed: {e}")
        return None

    # Small delay to let spot settle
    time.sleep(1)

    # Step 2: Short perp (same size as spot for delta neutrality)
    try:
        perp_result = exchange.market_open(
            name=coin,
            is_buy=False,  # Short
            sz=perp_size_coins,
        )
        print(f"Perp SHORT: {perp_size_coins} {coin} (market) | {perp_result}")

        status = perp_result.get("response", {}).get("data", {}).get("statuses", [{}])
        if status and "error" in str(status[0]):
            print(f"Perp order failed: {status}")
            # Try to unwind spot position
            print("Unwinding spot position...")
            try:
                exchange.order(
                    name=spot_symbol,
                    is_buy=False,
                    sz=spot_size_coins,
                    limit_px=round(mid_price * 0.999, 2 if mid_price < 100 else 0),
                    order_type={"limit": {"tif": "Ioc"}},
                )
            except Exception:
                pass
            return None

    except Exception as e:
        print(f"Perp short failed: {e}")
        # Try to unwind spot
        print("Unwinding spot position...")
        try:
            exchange.order(
                name=spot_symbol,
                is_buy=False,
                sz=spot_size_coins,
                limit_px=round(mid_price * 0.999, 2 if mid_price < 100 else 0),
                order_type={"limit": {"tif": "Ioc"}},
            )
        except Exception:
            pass
        return None

    position = DeltaNeutralPosition(
        coin=coin,
        spot_size=spot_size_coins,
        perp_size=perp_size_coins,
        spot_entry_price=spot_price,
        perp_entry_price=mid_price,
        entry_funding_rate=0,
        entry_timestamp=time.time(),
        tilt_pct=tilt_pct,
    )

    tilt_info = f" | tilt={tilt_pct*100:.0f}%" if tilt_pct > 0 else ""
    print(f"Delta-neutral opened: {coin} | delta={position.delta:.4f} ({position.delta_pct:.1f}%){tilt_info} | notional=${position.notional_usd:.2f}")
    return position


async def close_delta_neutral(
    exchange: Exchange,
    info: Info,
    position: DeltaNeutralPosition,
    vault_address: str = None,
) -> bool:
    """
    Close a delta-neutral position: sell spot + close perp short.
    """
    coin = position.coin
    print(f"\n--- Closing Delta-Neutral: {coin} ---")

    success = True

    # Step 1: Close perp short (market buy to cover)
    try:
        perp_result = exchange.market_close(coin=coin)
        print(f"Perp CLOSE: {coin} | {perp_result}")
    except Exception as e:
        print(f"Perp close failed: {e}")
        success = False

    # Step 2: Sell spot
    spot_symbol = PERP_TO_SPOT.get(coin, f"{coin}/USDC")
    try:
        all_mids = info.all_mids()
        mid_price = float(all_mids.get(coin, 0))
        sell_price = round(mid_price * 0.999, 2 if mid_price < 100 else 0)

        spot_result = exchange.order(
            name=spot_symbol,
            is_buy=False,
            sz=position.spot_size,
            limit_px=sell_price,
            order_type={"limit": {"tif": "Ioc"}},
        )
        print(f"Spot SELL: {position.spot_size} {spot_symbol} @ ${sell_price} | {spot_result}")
    except Exception as e:
        print(f"Spot sell failed: {e}")
        success = False

    return success


def check_delta_drift(
    position: DeltaNeutralPosition,
    info: Info,
    user_addr: str,
    api_url: str = HL_MAINNET_API,
) -> dict:
    """
    Check if delta has drifted beyond acceptable range.

    Returns:
        {"drifted": bool, "delta": float, "delta_pct": float, "action": str}
    """
    coin = position.coin

    # Get actual spot balance
    actual_spot = get_spot_balance(coin, info, user_addr, api_url)

    # Get actual perp position
    try:
        resp = requests.post(f"{api_url}/info", json={
            "type": "clearinghouseState", "user": user_addr
        }, timeout=10)
        user_state = resp.json()
        actual_perp = 0.0
        for pos_wrapper in user_state.get("assetPositions", []):
            pos = pos_wrapper.get("position", {})
            if pos.get("coin") == coin:
                actual_perp = abs(float(pos.get("szi", "0")))
                break
    except Exception:
        actual_perp = position.perp_size

    delta = actual_spot - actual_perp
    avg_size = (actual_spot + actual_perp) / 2 if (actual_spot + actual_perp) > 0 else 1
    delta_pct = abs(delta / avg_size) * 100 if avg_size > 0 else 0

    # >5% drift = needs rebalancing
    if delta_pct > 5:
        return {
            "drifted": True,
            "delta": delta,
            "delta_pct": delta_pct,
            "action": "rebalance",
            "spot": actual_spot,
            "perp": actual_perp,
        }

    return {
        "drifted": False,
        "delta": delta,
        "delta_pct": delta_pct,
        "action": "none",
        "spot": actual_spot,
        "perp": actual_perp,
    }


def format_dn_position(pos: DeltaNeutralPosition, current_price: float = 0) -> str:
    """Format delta-neutral position for logging."""
    spot_pnl = (current_price - pos.spot_entry_price) * pos.spot_size if current_price else 0
    perp_pnl = (pos.perp_entry_price - current_price) * pos.perp_size if current_price else 0
    net_pnl = spot_pnl + perp_pnl

    tilt_info = f" tilt={pos.tilt_pct*100:.0f}%" if pos.tilt_pct > 0 else ""
    return (
        f"{pos.coin} DN: spot={pos.spot_size:.4f} perp={pos.perp_size:.4f} "
        f"delta={pos.delta:.4f} ({pos.delta_pct:.1f}%){tilt_info} "
        f"notional=${pos.notional_usd:.2f} funding=${pos.cumulative_funding:.4f}"
        + (f" netPnL=${net_pnl:.4f}" if current_price else "")
    )
