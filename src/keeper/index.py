"""
Kodiak Keeper — Main event loop.
Ported from Yogi's keeper/index.ts for Hyperliquid.

Architecture:
- Connects to Hyperliquid via Python SDK
- Trades on behalf of a vault using agent wallet delegation
- Same intelligence layer as Yogi: regime engine + signal detector + imbalance scoring
- Adapted for Hyperliquid's hourly funding, higher leverage, and fee structure

Usage:
    python -m src.keeper.index
"""

import asyncio
import os
import sys
import time

from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants as hl_constants

from src.config.constants import HL_MAINNET_API, HL_TESTNET_API
from src.config.vault import STRATEGY_CONFIG
from src.keeper.regime_engine import (
    Regime,
    compute_regime,
    should_trigger_emergency_rebalance,
    format_regime,
    SIGNAL_NONE,
    SIGNAL_CRITICAL,
)
from src.keeper.leverage_controller import (
    LeverageState,
    classify_vol_regime,
    compute_target_leverage,
    fetch_reference_vol,
)
from src.keeper.signal_detector import (
    SignalState,
    detect_signals,
    format_signal_state,
)
from src.keeper.imbalance_detector import (
    MarketImbalance,
    fetch_market_imbalances,
    rank_by_imbalance,
    get_trade_direction,
)
from src.keeper.health_monitor import compute_health_state, compute_drawdown
from src.keeper.funding_scanner import (
    fetch_all_funding_rates,
    rank_markets_by_funding,
)
from src.keeper.position_manager import (
    BasisPosition,
    compute_target_allocations,
    open_basis_position,
    close_basis_position,
    should_exit_position,
)
from src.keeper.cost_calculator import evaluate_trade_economics, passes_cost_gate
from src.keeper.cross_venue_detector import (
    fetch_cross_venue_funding,
    format_cross_venue,
    get_cross_venue_adjustment,
)
from src.keeper.liquidation_detector import (
    detect_liquidations,
    format_liquidation_state,
)
from src.keeper.funding_preposition import (
    evaluate_all_settlements,
    format_settlements,
)
from src.keeper.delta_neutral import (
    DeltaNeutralPosition,
    open_delta_neutral,
    close_delta_neutral,
    check_delta_drift,
    format_dn_position,
)


def get_equity(info: Info, exchange: Exchange, vault_address: str | None, api_url: str) -> float:
    """
    Get total account equity in unified account mode.
    Unified mode: perp accountValue + spot USDC balance = total equity.
    """
    import requests
    user_addr = vault_address or exchange.wallet.address
    total = 0.0

    # Perp equity (includes margin + unrealized PnL)
    resp = requests.post(f"{api_url}/info", json={
        "type": "clearinghouseState", "user": user_addr
    }, timeout=10)
    state = resp.json()
    total += float(state.get("marginSummary", {}).get("accountValue", "0"))

    # Spot balances (USDC + any held tokens like HYPE for DN)
    resp2 = requests.post(f"{api_url}/info", json={
        "type": "spotClearinghouseState", "user": user_addr
    }, timeout=10)
    spot = resp2.json()

    # Get mid prices for non-USDC tokens
    all_mids = {}
    try:
        mid_resp = requests.post(f"{api_url}/info", json={"type": "allMids"}, timeout=10)
        all_mids = mid_resp.json()
    except Exception:
        pass

    for bal in spot.get("balances", []):
        coin = bal["coin"]
        amount = float(bal.get("total", 0))
        if amount <= 0:
            continue
        if coin == "USDC":
            total += amount
        else:
            # Convert non-USDC spot to USD using mid price
            mid_price = float(all_mids.get(coin, 0))
            if mid_price > 0:
                total += amount * mid_price

    return total


# --- Global State ---
active_positions: list[BasisPosition] = []
dn_positions: list[DeltaNeutralPosition] = []  # Delta-neutral positions
peak_equity: float = 0
current_leverage: LeverageState | None = None
latest_imbalances: list[MarketImbalance] = []
current_signals: SignalState = SignalState(
    severity=SIGNAL_NONE, events=[], timestamp=time.time(), market_snapshots=[]
)
current_regime: Regime | None = None


def init_hyperliquid(
    network: str = "testnet",
) -> tuple[Exchange, Info, str | None]:
    """
    Initialize Hyperliquid SDK connections.

    Returns (exchange, info, vault_address).
    """
    private_key = os.getenv("HL_PRIVATE_KEY")
    agent_key = os.getenv("HL_AGENT_PRIVATE_KEY")
    vault_address = os.getenv("HL_VAULT_ADDRESS") or None

    if not private_key:
        raise ValueError("HL_PRIVATE_KEY not set in .env")

    # Use agent key for signing if available, otherwise master key
    signing_key = agent_key or private_key
    account = Account.from_key(signing_key)

    base_url = HL_TESTNET_API if network == "testnet" else HL_MAINNET_API
    is_mainnet = network == "mainnet"

    # Fetch metadata manually to handle testnet spot metadata bugs
    import requests
    meta = requests.post(f"{base_url}/info", json={"type": "meta"}, timeout=10).json()
    try:
        spot_meta = requests.post(f"{base_url}/info", json={"type": "spotMeta"}, timeout=10).json()
        info = Info(base_url, skip_ws=True, meta=meta, spot_meta=spot_meta)
        exchange = Exchange(account, base_url, meta=meta, spot_meta=spot_meta,
                            vault_address=vault_address)
    except (IndexError, KeyError):
        # Testnet spot metadata can be malformed — use empty spot meta
        empty_spot = {"tokens": [], "universe": []}
        info = Info(base_url, skip_ws=True, meta=meta, spot_meta=empty_spot)
        exchange = Exchange(account, base_url, meta=meta, spot_meta=empty_spot,
                            vault_address=vault_address)

    master_account = Account.from_key(private_key)
    print(f"Master wallet: {master_account.address}")
    if agent_key:
        print(f"Agent wallet: {account.address}")
    if vault_address:
        print(f"Vault: {vault_address}")

    return exchange, info, vault_address


async def update_leverage(api_url: str) -> None:
    """Update vol regime and target leverage."""
    global current_leverage
    try:
        vol_bps = fetch_reference_vol(api_url)
        current_leverage = compute_target_leverage(vol_bps)
        print(
            f"Vol: {current_leverage.current_vol * 100:.1f}% "
            f"({current_leverage.regime} regime)"
        )
    except Exception as err:
        print(f"Failed to update leverage: {err}")


async def run_signal_detection(api_url: str) -> bool:
    """
    Run signal detection and update regime.
    Returns True if emergency rebalance should be triggered.
    """
    global current_signals, current_regime
    print("\n--- Signal Detection ---")
    try:
        current_signals = detect_signals(api_url=api_url)
        print(format_signal_state(current_signals))

        # Hyperliquid-specific: real liquidation detection
        liq_result = detect_liquidations(api_url=api_url)
        print(format_liquidation_state(liq_result))

        # Combine signal severity with liquidation severity.
        # Real liquidation data (zero-hash) supersedes the OI-drop proxy in
        # signal_detector since they measure the same phenomenon. When the real
        # liquidation detector has data, use it instead of double-counting.
        signal_severity_no_oi_proxy = max(
            (e.severity for e in current_signals.events if e.dimension != "liquidation_cascade"),
            default=SIGNAL_NONE,
        )
        if liq_result["max_severity"] > SIGNAL_NONE:
            # Real liquidation data available — use it, skip OI-drop proxy
            effective_severity = max(signal_severity_no_oi_proxy, liq_result["max_severity"])
        else:
            # No real liquidation data — fall back to full signal detector (including OI proxy)
            effective_severity = current_signals.severity

        if liq_result["cascade_detected"]:
            effective_severity = max(effective_severity, SIGNAL_CRITICAL)
            print("LIQUIDATION CASCADE: Escalating to CRITICAL")

        # Hyperliquid-specific: cross-venue funding comparison
        cross_venue = fetch_cross_venue_funding(api_url)
        print(format_cross_venue(cross_venue))

        vol_regime = (
            current_leverage.regime if current_leverage else classify_vol_regime(3000)
        )

        previous_regime = current_regime
        current_regime = compute_regime(vol_regime, effective_severity)
        print(f"Regime: {format_regime(current_regime)}")

        if should_trigger_emergency_rebalance(previous_regime, current_regime):
            prev_pct = previous_regime.deployment_pct if previous_regime else "?"
            print(
                f"REGIME SHIFT: Emergency rebalance triggered — "
                f"deployment {prev_pct}% → {current_regime.deployment_pct}%"
            )
            return True

        return False
    except Exception as err:
        print(f"Signal detection error: {err}")
        return False


async def run_imbalance_scan(api_url: str) -> None:
    """Scan markets for imbalance signals."""
    global latest_imbalances
    print("\n--- Imbalance Scan ---")
    try:
        all_imbalances = fetch_market_imbalances(api_url)
        latest_imbalances = rank_by_imbalance(all_imbalances)

        print(
            f"Scanned {len(all_imbalances)} markets -> "
            f"{len(latest_imbalances)} with tradeable signals"
        )

        for i, m in enumerate(latest_imbalances[:5]):
            dir_info = get_trade_direction(m)
            print(
                f"  {i+1}. {m.market}: signal={m.signal} ({m.signal_strength:.0f}%) | "
                f"premium={'+'if m.premium_pct>0 else ''}{m.premium_pct:.4f}% | "
                f"funding={m.annualized_funding_pct:.1f}% APY | "
                f"-> {dir_info['direction'].upper()} ({dir_info['reason']})"
            )
    except Exception as err:
        print(f"Imbalance scan error: {err}")


async def run_emergency_checks(
    info: Info,
    exchange: Exchange,
    vault_address: str | None,
    api_url: str = "",
) -> bool:
    """Run health and drawdown checks. Returns True if emergency triggered."""
    global peak_equity, active_positions, dn_positions, current_signals

    user_addr = vault_address or exchange.wallet.address

    has_positions = active_positions or dn_positions

    # Health check only applies when we have active positions
    if has_positions:
        import requests as _req
        resp = _req.post(f"{api_url}/info", json={
            "type": "clearinghouseState", "user": user_addr
        }, timeout=10)
        user_state = resp.json()
        health = compute_health_state(user_state)

        if health.action != "none":
            print(
                f"HEALTH {health.status.upper()}: ratio={health.margin_ratio:.3f} "
                f"equity=${health.total_equity:.2f} pnl=${health.unrealized_pnl:.2f}"
            )

            if health.action == "close_all":
                print("EMERGENCY: Closing all positions — health critical")
                for pos in reversed(active_positions):
                    await close_basis_position(exchange, info, pos.market, vault_address)
                active_positions.clear()
                return True

            if health.action == "reduce" and active_positions:
                print("WARNING: Reducing positions — health declining")
                largest = max(active_positions, key=lambda p: p.size_usd)
                await close_basis_position(exchange, info, largest.market, vault_address)
                active_positions.remove(largest)

    # Drawdown check
    equity = get_equity(info, exchange, vault_address, api_url)
    if equity < 0:
        print(f"CRITICAL: Negative equity detected (${equity:.2f}) — closing all")
        for pos in reversed(active_positions):
            await close_basis_position(exchange, info, pos.market, vault_address)
        active_positions.clear()
        return True

    # Reset peak when no positions (avoids stale peak from inflated margin readings)
    if not active_positions:
        peak_equity = equity
    elif equity > peak_equity:
        peak_equity = equity

    drawdown = compute_drawdown(equity, peak_equity)
    if drawdown["action"] != "none":
        print(
            f"DRAWDOWN {drawdown['drawdown_pct']:.2f}%: "
            f"equity=${equity:.2f} peak=${peak_equity:.2f}"
        )
        if drawdown["action"] == "close_all":
            print("EMERGENCY: Closing all positions — severe drawdown")
            for pos in reversed(active_positions):
                await close_basis_position(exchange, info, pos.market, vault_address)
            active_positions.clear()
            return True
        if drawdown["action"] == "reduce" and active_positions:
            print("WARNING: Reducing positions — drawdown limit")
            worst = active_positions[-1]
            await close_basis_position(exchange, info, worst.market, vault_address)
            active_positions.remove(worst)

    # Signal-driven emergency
    if current_signals.severity >= SIGNAL_CRITICAL and active_positions:
        print("SIGNAL CRITICAL: Reducing positions — anomaly detected")
        largest = max(active_positions, key=lambda p: p.size_usd)
        await close_basis_position(exchange, info, largest.market, vault_address)
        active_positions.remove(largest)

    return False


async def run_funding_scan(api_url: str) -> None:
    """Scan and display funding rates."""
    print("\n--- Funding Rate Scan ---")
    rates = fetch_all_funding_rates(api_url)
    ranked = rank_markets_by_funding(rates)

    cost_filtered = [m for m in ranked if passes_cost_gate(m.annualized_pct * 100)]

    print(
        f"Markets: {len(rates)} total -> {len(ranked)} positive funding "
        f"-> {len(cost_filtered)} cost-viable"
    )
    for i, m in enumerate(cost_filtered[:5]):
        econ = evaluate_trade_economics(m.annualized_pct * 100)
        print(
            f"  {i+1}. {m.market}: {m.annualized_pct:.2f}% APY "
            f"(net: {econ.net_profit_bps:.1f} bps/day, "
            f"break-even: {econ.break_even_hours:.0f}h)"
        )


async def run_dn_rebalance(
    exchange: Exchange,
    info: Info,
    vault_address: str | None,
    api_url: str,
) -> None:
    """Run delta-neutral rebalance cycle."""
    global dn_positions
    print("\n--- Rebalance Cycle (DELTA-NEUTRAL) ---")

    effective_leverage = (
        current_regime.max_leverage
        if current_regime
        else (current_leverage.target_leverage if current_leverage else 0)
    )
    deployment_pct = current_regime.deployment_pct if current_regime else 100

    # Close all if regime says zero
    if effective_leverage == 0 or deployment_pct == 0:
        mode = current_regime.rebalance_mode if current_regime else "unknown"
        print(f"Regime: {mode} — closing all DN positions")
        for pos in list(dn_positions):
            await close_delta_neutral(exchange, info, pos, vault_address)
        dn_positions.clear()
        return

    total_equity = get_equity(info, exchange, vault_address, api_url)
    deployable = total_equity * (deployment_pct / 100)
    mode = current_regime.rebalance_mode if current_regime else "unknown"

    print(f"Equity: ${total_equity:.2f} | Deployable: ${deployable:.2f} ({deployment_pct}%) | Mode: {mode}")

    # Check existing DN positions for exit signals (funding flipped)
    rates = fetch_all_funding_rates(api_url)
    rate_map = {r.market: r for r in rates}
    min_apy = STRATEGY_CONFIG.get("dn_min_funding_apy", 5.0)

    for pos in list(dn_positions):
        rate = rate_map.get(pos.coin)
        if rate and rate.annualized_pct < min_apy:
            print(f"Closing DN {pos.coin}: funding {rate.annualized_pct:.1f}% below {min_apy}% threshold")
            await close_delta_neutral(exchange, info, pos, vault_address)
            dn_positions.remove(pos)

    # Check delta drift on remaining positions
    user_addr = vault_address or exchange.wallet.address
    for pos in dn_positions:
        drift = check_delta_drift(pos, info, user_addr, api_url)
        if drift["drifted"]:
            print(f"Delta drift on {pos.coin}: {drift['delta_pct']:.1f}% (spot={drift['spot']:.4f} perp={drift['perp']:.4f})")

    # Find markets eligible for DN positions
    active_coins = {p.coin for p in dn_positions}
    ranked = [r for r in rank_markets_by_funding(rates) if r.annualized_pct >= min_apy]
    max_dn_positions = STRATEGY_CONFIG.get("max_markets_simultaneous", 3)

    if not ranked:
        print("No markets above minimum funding threshold for DN")
        return

    # Log existing positions
    for pos in dn_positions:
        mid = float(info.all_mids().get(pos.coin, 0))
        print(f"Holding: {format_dn_position(pos, mid)}")

    # Check for rotation: close positions whose market dropped below threshold
    # (already handled above in exit signals check)

    # How many new positions can we open?
    slots_available = max_dn_positions - len(dn_positions)
    if slots_available <= 0:
        # Check if any existing position should rotate to a much better market
        worst = min(dn_positions, key=lambda p: rate_map.get(p.coin, type('', (), {'annualized_pct': 0})()).annualized_pct if rate_map.get(p.coin) else 0)
        worst_rate = rate_map.get(worst.coin)
        best_new = next((r for r in ranked if r.market not in active_coins), None)

        if best_new and worst_rate and best_new.annualized_pct > worst_rate.annualized_pct * 2:
            print(f"Rotating: {worst.coin} ({worst_rate.annualized_pct:.1f}%) -> {best_new.market} ({best_new.annualized_pct:.1f}%)")
            await close_delta_neutral(exchange, info, worst, vault_address)
            dn_positions.remove(worst)
            slots_available = 1
        else:
            return

    # Calculate capital per new position
    # Total deployable minus capital already in existing DN positions
    capital_in_use = sum(p.notional_usd / 0.70 for p in dn_positions)  # Reverse the 70% to get total capital per pos
    remaining_capital = max(0, deployable - capital_in_use)

    new_markets = [r for r in ranked if r.market not in active_coins][:slots_available]
    if not new_markets or remaining_capital < 20:
        return

    capital_per_position = remaining_capital / len(new_markets)

    for market_data in new_markets:
        if capital_per_position < 20:  # Min $20 per DN position
            print(f"Skipping {market_data.market}: insufficient capital (${capital_per_position:.2f})")
            continue

        print(f"Opening DN: {market_data.market} at {market_data.annualized_pct:.1f}% APY | capital: ${capital_per_position:.2f}")

        pos = await open_delta_neutral(
            exchange, info, market_data.market, capital_per_position, vault_address, api_url
        )
        if pos:
            pos.entry_funding_rate = market_data.rate_hourly
            dn_positions.append(pos)


async def run_rebalance(
    exchange: Exchange,
    info: Info,
    vault_address: str | None,
    api_url: str,
) -> None:
    """Run the rebalance cycle."""
    global active_positions

    # Delta-neutral mode
    if STRATEGY_CONFIG.get("delta_neutral_mode", False):
        await run_dn_rebalance(exchange, info, vault_address, api_url)
        return

    print("\n--- Rebalance Cycle ---")

    effective_leverage = (
        current_regime.max_leverage
        if current_regime
        else (current_leverage.target_leverage if current_leverage else 0)
    )
    deployment_pct = current_regime.deployment_pct if current_regime else 100

    if effective_leverage == 0 or deployment_pct == 0:
        mode = current_regime.rebalance_mode if current_regime else "unknown"
        print(
            f"Regime: {mode} — closing all positions "
            f"(leverage={effective_leverage}x, deployment={deployment_pct}%)"
        )
        for pos in reversed(active_positions):
            await close_basis_position(exchange, info, pos.market, vault_address)
        active_positions.clear()
        return

    # 1. Check existing positions for exit signals
    rates = fetch_all_funding_rates(api_url)
    rate_map = {r.market: r for r in rates}

    for pos in list(active_positions):
        current_rate = rate_map.get(pos.market)
        if not current_rate:
            continue
        exit_info = should_exit_position(pos, current_rate.rate_hourly)
        if exit_info["exit"]:
            print(f"Exiting {pos.market}: {exit_info['reason']}")
            await close_basis_position(exchange, info, pos.market, vault_address)
            active_positions.remove(pos)

    # 2. Compute target allocations
    ranked = [m for m in rank_markets_by_funding(rates) if passes_cost_gate(m.annualized_pct * 100)]

    total_equity = get_equity(info, exchange, vault_address, api_url)

    deployable_equity = total_equity * (deployment_pct / 100)

    mode = current_regime.rebalance_mode if current_regime else "unknown"
    print(
        f"Equity: ${total_equity:.2f} | Deployable: ${deployable_equity:.2f} ({deployment_pct}%) | "
        f"Leverage: {effective_leverage}x | Mode: {mode}"
    )

    allocations = compute_target_allocations(deployable_equity, ranked, active_positions)
    basis_targets = allocations["basis_targets"]

    # Scale by regime-adjusted leverage
    for target in basis_targets:
        target["size_usd"] *= effective_leverage

    print(f"Basis targets: {len(basis_targets)} markets")

    # 3. Open new positions with imbalance-directed entry + cross-venue intelligence
    active_markets = {p.market for p in active_positions}
    imbalance_map = {m.market: m for m in latest_imbalances}

    # Fetch cross-venue funding for entry adjustment
    cross_venue = fetch_cross_venue_funding(api_url)
    cross_venue_map = {v.coin: v for v in cross_venue}

    for target in basis_targets:
        market = target["market"]
        if market in active_markets:
            continue
        if target["size_usd"] < 5:  # Min $5 on HL
            continue

        direction = "short"
        entry_reason = "funding positive -> short"

        if STRATEGY_CONFIG["use_imbalance_signals"]:
            imbalance = imbalance_map.get(market)
            if imbalance:
                trade = get_trade_direction(imbalance)
                if trade["direction"] == "none":
                    print(f"  Skipping {market}: {trade['reason']}")
                    continue
                direction = trade["direction"]
                entry_reason = trade["reason"]

        # Hyperliquid-specific: cross-venue funding adjustment
        cv = cross_venue_map.get(market)
        if cv:
            adj = get_cross_venue_adjustment(cv)
            if abs(adj["adjustment"]) > 0:
                entry_reason += f" | XV: {adj['reason']}"

        # In cautious/defensive mode, require stronger signals
        if current_regime and current_regime.rebalance_mode in ("cautious", "defensive"):
            imbalance = imbalance_map.get(market)
            min_strength = STRATEGY_CONFIG["cautious_min_signal_strength"]
            if imbalance and imbalance.signal_strength < min_strength:
                print(
                    f"  Skipping {market}: signal too weak for {current_regime.rebalance_mode} "
                    f"mode ({imbalance.signal_strength:.0f}% < {min_strength}%)"
                )
                continue

        try:
            print(f"  Opening {market}: {entry_reason}")
            await open_basis_position(
                exchange, info, market, target["size_usd"], direction, vault_address
            )

            rate_data = rate_map.get(market)
            active_positions.append(BasisPosition(
                market=market,
                direction=direction,
                size_usd=target["size_usd"],
                size_coin=target["size_usd"] / float(info.all_mids().get(market, 1)),
                entry_funding_rate=rate_data.rate_hourly if rate_data else 0,
                entry_timestamp=time.time(),
            ))
        except Exception as err:
            print(f"Failed to open position on {market}: {err}")


def load_existing_positions(
    info: Info, exchange: Exchange, vault_address: str | None, api_url: str
) -> None:
    """
    Load existing on-chain positions into in-memory state on startup.
    Prevents duplicate position opening after keeper restart.
    """
    global active_positions, dn_positions
    import requests as _req

    user_addr = vault_address or exchange.wallet.address
    print("--- Loading Existing On-Chain Positions ---")

    # Load perp positions
    resp = _req.post(f"{api_url}/info", json={
        "type": "clearinghouseState", "user": user_addr
    }, timeout=10)
    state = resp.json()

    perp_positions = {}
    for pos_wrapper in state.get("assetPositions", []):
        pos = pos_wrapper.get("position", {})
        szi = float(pos.get("szi", "0"))
        coin = pos.get("coin", "")
        if szi != 0:
            perp_positions[coin] = szi
            print(f"  Perp: {coin} size={szi} entryPx={pos.get('entryPx')}")

    # Load spot positions
    resp2 = _req.post(f"{api_url}/info", json={
        "type": "spotClearinghouseState", "user": user_addr
    }, timeout=10)
    spot_state = resp2.json()

    spot_positions = {}
    for b in spot_state.get("balances", []):
        coin = b.get("coin", "")
        total = float(b.get("total", 0))
        if total > 0.001 and coin != "USDC":
            spot_positions[coin] = total
            print(f"  Spot: {coin} balance={total}")

    if not perp_positions and not spot_positions:
        print("  No existing positions found.")
        return

    # Reconstruct DN positions (where we have both spot and perp on same asset)
    if STRATEGY_CONFIG.get("delta_neutral_mode", False):
        for coin, spot_size in spot_positions.items():
            perp_size = abs(perp_positions.get(coin, 0))
            if perp_size > 0:
                # Found matching spot + perp = DN position
                mid = float(info.all_mids().get(coin, 0))
                tilt_pct = (perp_size - spot_size) / spot_size if spot_size > 0 else 0
                dn_pos = DeltaNeutralPosition(
                    coin=coin,
                    spot_size=spot_size,
                    perp_size=perp_size,
                    spot_entry_price=mid,
                    perp_entry_price=mid,
                    entry_funding_rate=0,
                    entry_timestamp=time.time(),
                    tilt_pct=max(0, tilt_pct),
                )
                dn_positions.append(dn_pos)
                print(f"  Restored DN: {coin} spot={spot_size:.4f} perp={perp_size:.4f} delta={dn_pos.delta:.4f} ({dn_pos.delta_pct:.1f}%)")
                # Remove from perp_positions so we don't double-count
                del perp_positions[coin]

        # Remaining unmatched perps = directional positions (shouldn't exist in DN mode)
        for coin, szi in perp_positions.items():
            print(f"  WARNING: Unmatched perp {coin} size={szi} — closing to avoid unhedged exposure")
            try:
                exchange.market_close(coin=coin)
                print(f"  Closed {coin}")
            except Exception as e:
                print(f"  Failed to close {coin}: {e}")
    else:
        # Directional mode — load perps as active_positions
        for coin, szi in perp_positions.items():
            direction = "short" if szi < 0 else "long"
            mid = float(info.all_mids().get(coin, 0))
            active_positions.append(BasisPosition(
                market=coin,
                direction=direction,
                size_usd=abs(szi) * mid,
                size_coin=abs(szi),
                entry_funding_rate=0,
                entry_timestamp=time.time(),
            ))
            print(f"  Restored: {coin} {direction} size={abs(szi)}")

    print(f"  Loaded: {len(dn_positions)} DN + {len(active_positions)} directional positions\n")


async def main() -> None:
    """Kodiak keeper main loop."""
    load_dotenv()

    print("Kodiak Keeper Starting...")
    print("Strategy: Hyperliquid basis trade alpha + intelligent signal detection")
    print("Intelligence: OI shift, liquidation cascade, funding vol, spread blow-out")
    print("Regime: Vol regime × signal severity -> adaptive deployment + leverage\n")

    network = os.getenv("HL_NETWORK", "testnet")
    exchange, info, vault_address = init_hyperliquid(network)
    api_url = HL_MAINNET_API if network == "mainnet" else HL_TESTNET_API

    print(f"Network: {network}")
    print(f"API: {api_url}")
    print("Connected.\n")

    # Load existing on-chain positions to prevent duplicates after restart
    try:
        load_existing_positions(info, exchange, vault_address, api_url)
    except Exception as e:
        print(f"Warning: Failed to load existing positions: {e}")

    # Set up dead man's switch (auto-cancel orders if keeper goes offline)
    # HL supports scheduleCancel — cancel all orders if no heartbeat for N seconds
    try:
        exchange.schedule_cancel(time=int(time.time() + 3600))  # 1 hour timeout
        print("Dead man's switch set (1 hour)")
    except Exception as e:
        print(f"Failed to set dead man's switch: {e}")

    # Initialize all systems
    await update_leverage(api_url)
    await run_signal_detection(api_url)
    await run_imbalance_scan(api_url)
    await run_funding_scan(api_url)

    last_scan = time.time()
    last_rebalance = 0.0
    last_emergency_check = 0.0
    last_leverage_update = time.time()
    last_signal_detection = time.time()

    while True:
        now = time.time()
        now_ms = now * 1000

        # Emergency checks (every 30s)
        if now_ms - last_emergency_check * 1000 >= STRATEGY_CONFIG["emergency_check_interval_ms"]:
            try:
                emergency = await run_emergency_checks(info, exchange, vault_address, api_url)
                if emergency:
                    print("Emergency triggered — pausing rebalance for 5 minutes")
                    last_rebalance = now
            except Exception as err:
                print(f"Emergency check error: {err}")
            last_emergency_check = now

        # Signal detection (every 5 min)
        if now_ms - last_signal_detection * 1000 >= STRATEGY_CONFIG["signal_detection_interval_ms"]:
            emergency_rebalance = await run_signal_detection(api_url)
            if emergency_rebalance and active_positions:
                try:
                    await run_rebalance(exchange, info, vault_address, api_url)
                except Exception as err:
                    print(f"Emergency rebalance error: {err}")
                last_rebalance = now
            elif emergency_rebalance:
                print("Regime shift detected but no positions to rebalance — skipping")
            last_signal_detection = now

        # Leverage + imbalance update (every 30 min)
        if now_ms - last_leverage_update * 1000 >= STRATEGY_CONFIG["funding_scan_interval_ms"]:
            await update_leverage(api_url)
            await run_imbalance_scan(api_url)
            last_leverage_update = now

        # Funding scan (every 30 min)
        if now_ms - last_scan * 1000 >= STRATEGY_CONFIG["funding_scan_interval_ms"]:
            try:
                await run_funding_scan(api_url)
            except Exception as err:
                print(f"Funding scan error: {err}")
            last_scan = now

        # Rebalance (every 4 hours)
        if now_ms - last_rebalance * 1000 >= STRATEGY_CONFIG["rebalance_interval_ms"]:
            try:
                await run_rebalance(exchange, info, vault_address, api_url)
            except Exception as err:
                print(f"Rebalance error: {err}")
            last_rebalance = now

        # Funding pre-positioning check (every 30s — needs to be frequent near settlement)
        try:
            current_pos_map = {p.market: p.direction for p in active_positions}
            settlements = evaluate_all_settlements(
                current_positions=current_pos_map, api_url=api_url
            )

            # Only log when near settlement (within pre-positioning window)
            actionable = [s for s in settlements if s.optimal_action != "wait"]
            if actionable:
                print(format_settlements(settlements))

                for s in actionable:
                    if s.optimal_action == "exit" and s.coin in current_pos_map:
                        # Exit position that's paying funding at settlement
                        print(f"  Pre-position EXIT: {s.reason}")
                        await close_basis_position(exchange, info, s.coin, vault_address)
                        active_positions[:] = [p for p in active_positions if p.market != s.coin]
        except Exception as err:
            print(f"Pre-positioning error: {err}")

        # Refresh dead man's switch
        try:
            exchange.schedule_cancel(time=int(time.time() + 3600))
        except Exception:
            pass

        # Heartbeat
        equity = get_equity(info, exchange, vault_address, api_url)

        severity_labels = ["CLEAR", "LOW", "HIGH", "CRITICAL"]
        mode = current_regime.rebalance_mode if current_regime else "?"
        deploy = current_regime.deployment_pct if current_regime else "?"
        lev = current_regime.max_leverage if current_regime else "?"
        signal_label = severity_labels[current_signals.severity] if current_signals.severity < 4 else "?"
        next_rebalance_min = round(
            (STRATEGY_CONFIG["rebalance_interval_ms"] - (now_ms - last_rebalance * 1000)) / 60000
        )

        pos_count = len(dn_positions) if STRATEGY_CONFIG.get("delta_neutral_mode") else len(active_positions)
        dn_tag = " [DN]" if STRATEGY_CONFIG.get("delta_neutral_mode") else ""
        print(
            f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}]{dn_tag} "
            f"Positions: {pos_count} | "
            f"Equity: ${equity:.2f} | "
            f"Regime: {mode} ({deploy}% @ {lev}x) | "
            f"Signal: {signal_label} | "
            f"Next rebalance: {next_rebalance_min}min"
        )

        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
