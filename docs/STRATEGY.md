# Kodiak Vault — Strategy Documentation

## Thesis

Hyperliquid's deep orderbook and high-leverage environment (up to 50x) creates structural inefficiencies — funding rate dislocations, mark/oracle premium, and aggressive liquidation cascades — that mean-revert predictably. Traditional basis vaults capture these with funding rate harvesting. Kodiak adds a second dimension: **forward-looking anomaly detection** that senses market stress before vol-based indicators react.

**Core insight**: Vol-based leverage scaling is reactive — it reduces exposure *after* volatility has already spiked. By then, slippage is high, liquidity is thin, and drawdowns have already occurred. Kodiak's signal detector monitors leading indicators (OI shifts, liquidation cascades, funding instability, spread blow-outs) that precede vol spikes, enabling proactive position reduction.

**Revenue sources**: Funding payments + mark/oracle premium convergence + maker execution advantage. Three sources active across all market conditions, with deployment scaled by regime intelligence.

**Why Hyperliquid**: Compared to Drift (where Yogi operates), Hyperliquid has deeper orderbooks, more aggressive trader behavior (higher leverage = more liquidation cascades = bigger funding dislocations), native vault infrastructure, and a built-in dead man's switch. The protocol's aggressive trading culture creates exactly the kind of dislocations a funding rate + anomaly detection strategy thrives on.

## How It Works

### Capital Allocation

```
Total Capital
|
+-- 100% --> Regime-Adaptive Arbitrage Pool
             |
             +-- Signal Detector (every 5 min)
             |   +-- Fetches all Hyperliquid perp markets
             |   +-- Computes 4 anomaly dimensions:
             |   |   1. OI imbalance shift (mass repositioning)
             |   |   2. Liquidation cascade (OI drop proxy)
             |   |   3. Funding rate volatility (regime transition)
             |   |   4. Spread blow-out (mark/oracle stress)
             |   +-- Max severity across dimensions = aggregate signal
             |   --> CLEAR (0) / LOW (1) / HIGH (2) / CRITICAL (3)
             |
             +-- Regime Engine
             |   +-- Reads vol regime (Parkinson estimator on BTC)
             |   +-- Reads signal severity (from detector)
             |   +-- Looks up deployment matrix:
             |   |   volRegime x signalSeverity --> deploymentPct + maxLeverage
             |   +-- Determines rebalanceMode:
             |       aggressive / normal / cautious / defensive
             |
             +-- Imbalance Detector (every 30 min)
             |   +-- Reads mark/oracle premium (price deviation)
             |   +-- Reads funding rate (hourly)
             |   +-- Estimates OI imbalance from funding direction
             |   +-- Composite: 50% funding + 30% premium + 20% OI
             |   --> Signal strength + direction (SHORT/LONG/SKIP)
             |
             +-- Position Manager
                 +-- Applies deployment % from regime engine
                 +-- Scales by regime-adjusted leverage
                 +-- Uses maker limit orders (1.5 bps)
                 +-- 7-day minimum hold, max 2 rotations/week
```

Unlike Yogi which allocates 30% to a lending floor (Kamino/Marginfi), Kodiak deploys 100% to perps because Hyperliquid does not have native lending protocols. Idle capital sits as USDC collateral.

### Signal Detection Pipeline

The signal detector runs every 5 minutes — 6x faster than the funding scan. Each dimension independently classifies severity:

#### 1. OI Imbalance Shift

Measures how fast the funding-implied imbalance is changing across monitored markets. Rapid shifts signal mass repositioning — often preceding funding rate spikes or liquidation cascades.

- Compares current snapshot to oldest in rolling 1-hour history
- Thresholds: 4% (LOW), 12% (HIGH), 25% (CRITICAL)
- Tighter than Yogi (5/15/30%) because Hyperliquid's higher leverage amplifies cascade speed

#### 2. Liquidation Cascade

Proxied by sudden OI drop. When OI decreases rapidly without corresponding price recovery, it indicates forced liquidations — margin calls cascading through the system. Hyperliquid's up-to-50x leverage makes cascades more violent than on Drift.

- Measures percentage OI drop over rolling 1-hour window
- Thresholds: 4% (LOW), 12% (HIGH), 25% (CRITICAL)

#### 3. Funding Rate Volatility

Unstable funding rates signal regime transitions. When funding whipsaws between positive and negative, directional strategies face increased uncertainty. Hyperliquid settles funding hourly (vs continuous on Drift), making rate volatility more discrete.

- Rolling 24-entry standard deviation, annualized to bps
- Thresholds: 500 bps (LOW), 1500 bps (HIGH), 3000 bps (CRITICAL)

#### 4. Spread Blow-out

Mark/oracle divergence across markets indicates thin liquidity, forced selling, or price manipulation. Large spreads precede costly rebalances.

- Max absolute mark/oracle spread across monitored markets
- Thresholds: 0.3% (LOW), 1.0% (HIGH), 2.5% (CRITICAL)
- Tighter than Yogi (0.5/1.5/3.0%) because Hyperliquid's orderbook should maintain tighter spreads in normal conditions

### Regime Engine Decision Matrix

The regime engine combines vol regime (backward-looking) with signal severity (forward-looking):

```
                   Signal Severity
Vol Regime    CLEAR    LOW      HIGH     CRITICAL
-------------------------------------------------
Very Low     100/2.0  80/1.5   50/1.0   25/0.5
Low           85/1.5  70/1.2   40/0.8   20/0.3
Normal        70/1.0  55/0.8   30/0.5   15/0.2
High          50/0.5  35/0.3   20/0.2   10/0.0
Extreme        0/0.0   0/0.0    0/0.0    0/0.0

Format: deploymentPct / maxLeverage
```

**Rebalance modes** derived from deployment percentage:
- **Aggressive** (>= 85%): Normal entry thresholds, full deployment
- **Normal** (55-84%): Standard operation
- **Cautious** (20-54%): Requires 40%+ signal strength for new entries
- **Defensive** (< 20%): Minimal positions, close-only mode

**Emergency rebalance** triggered when:
- Deployment drops 30%+ in a single detection cycle
- Signal severity jumps from CLEAR/LOW to CRITICAL
- Mode transitions to defensive

### Entry Criteria

A market is eligible for a position when ALL of the following are met:
1. Composite signal strength >= 20% (40% in cautious/defensive mode)
2. Market is on the allowed whitelist (BTC, ETH, SOL, HYPE) and not excluded
3. Cost gate passes: expected funding over 7-day hold > round-trip maker costs (5 bps)
4. Regime allows deployment > 0% and leverage > 0
5. Position size >= $10 (Hyperliquid minimum notional)

### Direction Logic

```
IF funding > 0 AND mark > oracle --> SHORT (collect funding + premium convergence)
IF funding < 0 AND mark < oracle --> LONG (collect funding + discount convergence)
IF signals conflict             --> SKIP (composite near zero = no conviction)
```

On Hyperliquid, long/short OI split is not directly exposed. The imbalance detector estimates OI direction from funding rate sign and magnitude, combined with mark/oracle premium.

### Exit Criteria

A position is closed when ANY of the following occur:
1. Funding rate drops below -0.5% exit threshold
2. Portfolio drawdown exceeds 3% (reduce) or 5% (close all)
3. Margin ratio drops below 1.15 (reduce) or 1.08 (emergency close all)
4. Regime transitions to 0% deployment or 0x leverage
5. Signal severity reaches CRITICAL (force reduce largest position)
6. Negative equity detected (emergency close all)

## Risk Management

### Dynamic Leverage (Vol-Based)

| Vol Regime | Realized Vol | Base Leverage | Rationale |
|------------|-------------|---------------|-----------|
| Very Low | < 20% | 2.0x | Calm markets, safe for moderate leverage |
| Low | 20-35% | 1.5x | Normal conditions |
| Normal | 35-50% | 1.0x | Elevated — conservative |
| High | 50-75% | 0.5x | Turbulent — minimal exposure |
| Extreme | > 75% | 0x | Shut down |

**Signal override**: Signal severity can reduce leverage below vol-based level. At LOW signal + veryLow vol, leverage drops from 2.0x to 1.5x. At CRITICAL + any vol, leverage is near zero.

### Margin Ratio Monitoring

| Level | Margin Ratio | Action |
|-------|-------------|--------|
| Healthy | > 1.15 | Normal operation |
| Warning | 1.08 – 1.15 | Reduce largest position |
| Critical | < 1.08 | Emergency close all |
| Liquidatable | < 1.0 | Hyperliquid liquidates (should never reach this) |

Monitored every **30 seconds** — 480x more frequent than the 4-hour rebalance.

### Signal-Driven Risk

| Signal Level | Keeper Action |
|-------------|---------------|
| CLEAR | Normal operation, full deployment |
| LOW | Reduce deployment to 70-85%, lower leverage |
| HIGH | Reduce to 20-50%, cautious mode (40% entry threshold) |
| CRITICAL | Reduce to 10-25%, force-close largest position |

### Position Sizing

| Parameter | Value |
|-----------|-------|
| Basis pool | 100% (scaled by deployment %) |
| Max per market | 40% |
| Max markets | 3 |
| Max leverage | 2x (hard ceiling, despite HL allowing 50x) |
| Minimum notional | $10 (Hyperliquid minimum) |

### Drawdown Management

- **3% drawdown**: Reduce positions — close worst-performing
- **5% drawdown**: Emergency close all — sit in idle USDC
- **Negative equity**: Emergency close all

### Dead Man's Switch

Kodiak sets a `scheduleCancel` on Hyperliquid every heartbeat cycle. If the keeper goes offline for more than 1 hour, all open orders are automatically cancelled by the protocol. This is a native Hyperliquid feature not available on Drift.

### What We Don't Do

- **No leverage looping** — No borrowing against collateral recursively
- **No DEX LP** — No impermanent loss exposure (no JLP, HLP, etc.)
- **No yield-bearing stables** — No circular yield dependencies
- **No illiquid altcoins** — Max 3 markets, all must pass OI filters ($5M minimum)
- **No fixed leverage** — Leverage adapts to vol AND signals (2x max despite 50x available)
- **No blind deployment** — Signal detector prevents full exposure during building stress

## Expected Returns

| Market Condition | Vol | Signals | Deployment | Direction | Expected APY |
|-----------------|-----|---------|------------|-----------|-------------|
| Bull (longs dominant) | Low | CLEAR | 100% @ 2.0x | SHORT | 20-30% |
| Neutral | Normal | CLEAR | 70% @ 1.0x | Signal-based | 15-25% |
| Bear (shorts dominant) | Normal | LOW | 55% @ 0.8x | LONG | 8-15% |
| Bear + contagion | Normal | CRITICAL | 15% @ 0.2x | Minimal | 2-5% |
| Crisis (extreme vol) | Extreme | Any | 0% @ 0.0x | None | 0% (idle) |
| Recovery | Low | CLEAR | 100% @ 2.0x | Signal-based | 20-30% |

### Yield Stack

| Source | Where | Est. APY Contribution |
|--------|-------|----------------------|
| Funding harvesting | Hyperliquid perps (bidirectional, hourly settlement) | 8-15% |
| Premium convergence | Mark/oracle mean reversion | 2-5% |
| Maker execution | Limit orders reduce cost vs taker (5 bps vs 11 bps RT) | 0.5-1% |
| **Total** | | **15-25% (normal) / 8-15% (hostile)** |

### Hyperliquid Fee Economics

| | Taker | Maker (Kodiak) |
|---|---|---|
| Hyperliquid fee | 4.5 bps | 1.5 bps |
| Estimated slippage | 1 bps | 1 bps |
| Per-trade cost | 5.5 bps | 2.5 bps |
| Round-trip cost | 11 bps | 5 bps |
| Break-even (7-day hold) | 5.73% APY | 2.61% APY |

At higher volume tiers, maker fees drop further (0% at >$500M 14-day volume) with potential rebates.

## Known Limitations

1. **OI imbalance estimation** — Hyperliquid does not expose long/short OI split directly. We estimate imbalance from funding rate direction and magnitude. This is a proxy, not ground truth.
2. **No lending floor** — Unlike Yogi which earns 1.5-6.5% APY on idle capital via Kamino/Marginfi, Kodiak's idle USDC earns nothing. Capital efficiency depends entirely on perp deployment.
3. **Signal detection building track record** — The anomaly detector is adapted from Yogi's Drift-tuned thresholds. Hyperliquid's different microstructure may require threshold adjustments after live observation.
4. **Regime matrices are manually tuned** — The 5x4 deployment/leverage matrices were designed from first principles, not optimized from Hyperliquid historical data.
5. **Single-keeper architecture** — No multi-reporter consensus. The keeper is a single point of trust, mitigated by the dead man's switch.
6. **Unified account mode** — Hyperliquid's unified account mode shares collateral between spot and perp. Equity calculation must sum both to avoid false drawdown triggers.

## Implementation Details

### Technology

- **Vault infrastructure**: Hyperliquid native vaults — 10% profit share, 1-day depositor lockup, leader maintains >= 5% ownership
- **Trading**: Hyperliquid L1 — perpetual futures execution via agent wallet delegation
- **Keeper**: Python bot on AWS EC2 with pm2 (24/7, auto-restart on reboot)
- **SDK**: hyperliquid-python-sdk (official, actively maintained)
- **Signal detection**: 4-dimension anomaly detector with configurable thresholds
- **Vol computation**: Parkinson estimator on BTC hourly candles (168 samples = 7 days)
- **Data feed**: Hyperliquid REST API (metaAndAssetCtxs, candleSnapshot, fundingHistory)

### Keeper Loop Architecture

```
Main Loop (30-second tick)
+-- Every 30s:  Emergency checks (margin ratio + drawdown + signal severity)
+-- Every 5m:   Signal detection (4 dimensions) + regime update
|               --> Emergency rebalance if regime shifts dramatically
+-- Every 30m:  Funding scan + leverage update + imbalance scan
+-- Every 4h:   Full rebalance cycle
|   +-- Apply regime-adjusted deployment %
|   +-- Scale targets by regime-adjusted leverage
|   +-- Weight allocation by annualized funding rate
|   +-- Require 40%+ signal strength in cautious/defensive mode
|   +-- Close underperforming positions
|   +-- Open new positions in top 3 markets
+-- Every 30s:  Heartbeat log (equity, regime, signal, deployment)
+-- Every 30s:  Refresh dead man's switch (1-hour auto-cancel)
```

### Execution Flow

1. **Deposit**: USDC bridged to Hyperliquid via Arbitrum or CCTP, deposited to vault
2. **Delegation**: Agent wallet authorized to trade on behalf of master/vault
3. **Signal check**: Keeper runs 4-dimension anomaly detection every 5 minutes
4. **Regime compute**: Vol regime x signal severity --> deployment + leverage
5. **Cost check**: Keeper evaluates each market's funding vs. trading costs
6. **Trading**: Keeper places SHORT/LONG perp limit orders (size = allocation x deployment% x leverage)
7. **Monitoring**: 30-second margin checks; 5-minute signal scans; dead man's switch refresh
8. **Regime shift**: If signals spike, emergency rebalance reduces exposure immediately
9. **Funding**: Positions accumulate funding payments hourly (settled by protocol)
10. **Withdrawal**: Leader/depositor withdraws USDC (1-day lockup for depositors)

### Lineage

Kodiak is a port of [Yogi](https://github.com/psyto/yogi) from Drift/Solana to Hyperliquid. The strategy brain (regime engine, 4D signal detector, imbalance scoring, cost calculator) is identical in logic. Key adaptations:

| Component | Yogi (Drift) | Kodiak (Hyperliquid) |
|-----------|---|---|
| Language | TypeScript | Python |
| Funding | Continuous settlement | Hourly settlement |
| Vol reference | SOL-PERP candles | BTC candles |
| OI data | Direct long/short split from Drift API | Estimated from funding direction |
| Maker fees | -0.2 bps (rebate) | 1.5 bps |
| Lending floor | 30% to Kamino/Marginfi | None |
| Signal thresholds | OI shift 5/15/30%, spread 0.5/1.5/3.0% | OI shift 4/12/25%, spread 0.3/1.0/2.5% |
| Dead man's switch | Not native | Native scheduleCancel |
| Vault | Voltr (Ranger Earn) | Hyperliquid native |
