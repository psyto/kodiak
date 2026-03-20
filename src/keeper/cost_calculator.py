"""
Cost Calculator — trade economics for Kodiak.
Ported from Yogi's cost-calculator.ts.

Adjusted for Hyperliquid fee structure:
  Base tier: Taker 4.5bps, Maker 1.5bps (no rebate at base tier)

  With maker orders:
    Round-trip cost = 2 × (slippage + makerFee) = 2 × (1 + 1.5) = 5 bps
  With taker orders (fallback):
    Round-trip cost = 2 × (slippage + takerFee) = 2 × (1 + 4.5) = 11 bps

  Break-even hold (7-day):
    Maker: 5 × 8760 / 168 = 261 bps (2.61% APY)
    Taker: 11 × 8760 / 168 = 573 bps (5.73% APY)
"""

from dataclasses import dataclass

from src.config.vault import STRATEGY_CONFIG


@dataclass
class TradeEconomics:
    expected_funding_bps: float
    holding_period_hours: float
    profitable: bool
    net_profit_bps: float
    round_trip_cost_bps: float
    break_even_hours: float
    order_type: str  # "maker" | "taker"


def evaluate_trade_economics(
    annualized_funding_bps: float,
    estimated_hold_hours: float = 0,
) -> TradeEconomics:
    """Compute whether a basis trade is profitable after costs."""
    if estimated_hold_hours == 0:
        estimated_hold_hours = STRATEGY_CONFIG["min_holding_period_hours"]

    slippage = STRATEGY_CONFIG["estimated_slippage_bps"]
    use_limit = STRATEGY_CONFIG["use_limit_orders"]
    maker_fee = STRATEGY_CONFIG["hl_maker_fee_bps"]
    taker_fee = STRATEGY_CONFIG["hl_taker_fee_bps"]

    per_trade_cost = (slippage + maker_fee) if use_limit else (slippage + taker_fee)
    round_trip_cost = 2 * per_trade_cost

    hours_per_year = 8760
    expected_funding_earned = (annualized_funding_bps * estimated_hold_hours) / hours_per_year
    net_profit_bps = expected_funding_earned - round_trip_cost
    profitable = net_profit_bps > 0

    break_even_hours = (
        (round_trip_cost * hours_per_year) / annualized_funding_bps
        if annualized_funding_bps > 0
        else float("inf")
    )

    return TradeEconomics(
        expected_funding_bps=annualized_funding_bps,
        holding_period_hours=estimated_hold_hours,
        profitable=profitable,
        net_profit_bps=net_profit_bps,
        round_trip_cost_bps=round_trip_cost,
        break_even_hours=break_even_hours,
        order_type="maker" if use_limit else "taker",
    )


def passes_cost_gate(annualized_funding_bps: float) -> bool:
    """Check if a trade passes the profitability threshold."""
    economics = evaluate_trade_economics(annualized_funding_bps)
    return economics.profitable
