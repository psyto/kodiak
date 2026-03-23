# Kodiak Vault — Strategy Documentation

## Thesis

Hyperliquid's deep orderbook and high-leverage environment (up to 50x) creates persistent funding rate dislocations — particularly on native assets like HYPE where no CEX arbitrage exists. Kodiak captures this through **delta-neutral positions** (simultaneous spot buy + perp short) that eliminate price risk entirely, collecting pure funding yield.

**Core insight**: Directional funding harvesting (just shorting perps) exposes you to price risk — a single price spike can wipe out weeks of funding income. Delta-neutral execution removes this: spot gains offset perp losses and vice versa. Profit comes purely from the funding rate spread between the two legs.

**Revenue sources**: Hourly funding payments on delta-neutral positions + funding pre-positioning timing alpha. Deployment scaled by a 6-dimension anomaly detection engine that reduces exposure before market stress.

**Why Hyperliquid**: Compared to Drift (where Yogi operates), Hyperliquid has deeper orderbooks, more aggressive trader behavior (higher leverage = more liquidation cascades = bigger funding dislocations), native vault infrastructure, and a built-in dead man's switch. The protocol's aggressive trading culture creates exactly the kind of dislocations a funding rate + anomaly detection strategy thrives on.

## How It Works

### Capital Allocation

```
Total Capital ($220 USDC)
|
+-- Regime Engine decides deployment % (0-100%)
|
+-- Delta-Neutral Engine (when funding > 5% APY)
|   +-- 70% of deployable --> Buy SPOT (e.g., 2.94 HYPE)
|   +-- 30% of deployable --> Perp SHORT margin (same 2.94 HYPE)
|   +-- Price movement cancels: spot gain = perp loss
|   +-- Collect pure funding yield hourly
|   +-- Monitor delta drift (rebalance if >5%)
|   +-- Auto-rotate to higher-yielding asset (if >2x better)
|
+-- Signal Detector (every 5 min)
|   +-- 4 anomaly dimensions + real liquidation data
|   +-- Autocorrelation-aware (real data supersedes proxy)
|   --> CLEAR / LOW / HIGH / CRITICAL
|
+-- Cross-Venue Detector (every 5 min)
|   +-- HL vs Binance vs Bybit funding + OI
|   --> Entry timing adjustment
|
+-- Regime Engine
|   +-- Vol regime x signal severity --> deployment %
|   --> Close all DN positions if deployment = 0%
                 +-- Uses maker limit orders (1.5 bps)
                 +-- 7-day minimum hold, max 2 rotations/week
```

Unlike Yogi which allocates 30% to a lending floor (Kamino/Marginfi), Kodiak deploys 100% to perps because Hyperliquid does not have native lending protocols. Idle capital sits as USDC collateral.

### Delta-Neutral Execution

Kodiak opens **paired positions** with an optional **directional tilt** — a feature unique to Kodiak on Hyperliquid (neither Liminal nor Harmonix offer this):

| Component | Allocation | Purpose |
|-----------|-----------|---------|
| Spot buy | 70% of deployable capital | Price hedge |
| Perp short margin | 30% of deployable capital | Funding collection |
| Net delta | **-10% (configurable)** | Slight short bias for extra yield |

**Pure DN vs Tilted DN:**
```
Pure DN:    spot=1.65 HYPE + perp=-1.65 HYPE  →  delta=0      (zero price risk)
Tilted DN:  spot=1.30 HYPE + perp=-1.43 HYPE  →  delta=-10%   (short bias)
```

**Example (live, 2026-03-22):**
```
Capital: $64.00 deployable
Spot BUY: 1.30 HYPE/USDC @ $38.25 (filled)
Perp SHORT: 1.43 HYPE @ $38.26 (filled, 10% larger than spot)
Delta: -0.13 (-10.0%) | tilt=10%
Funding: +10.9% APY on $54.65 notional (perp leg)
```

**Why tilt?**
- **More funding:** 10% larger perp = 10% more funding income
- **Directional upside:** In bearish markets (current), the short bias profits from price drops
- **Limited downside:** HYPE must pump >10.9% (one year of funding yield) before the tilt loses money vs pure DN
- **Configurable:** `dn_tilt_pct: 0.10` in config. Set to 0.0 for pure DN at any time

**Why 70/30?** The perp short requires margin but not the full notional value. 30% margin supports a 1x short position with comfortable headroom. The remaining 70% buys spot — maximizing the hedged notional.

**Delta drift monitoring:** Every signal detection cycle checks that spot and perp sizes still match. If delta drifts beyond 5% (from partial fills, liquidation, or rounding), the keeper rebalances.

**Auto-rotation:** If a different asset's funding rate exceeds the current position's by >2x, the keeper closes the current DN position and opens a new one on the higher-yielding asset.

**Competitive positioning:** No other Hyperliquid vault offers tilted DN. Liminal ($30M TVL) and Harmonix ($6M TVL) both run pure DN (delta=0) only. Kodiak's configurable tilt is a unique differentiator for depositors who want funding yield with optional directional exposure.

### Signal Detection Pipeline

The signal detector runs every 5 minutes — 6x faster than the funding scan. Each dimension independently classifies severity:

#### 1. OI Imbalance Shift

Measures how fast the funding-implied imbalance is changing across monitored markets. Rapid shifts signal mass repositioning — often preceding funding rate spikes or liquidation cascades.

- Compares current snapshot to oldest in rolling 1-hour history
- Thresholds: 4% (LOW), 12% (HIGH), 25% (CRITICAL)
- Tighter than Yogi (5/15/30%) because Hyperliquid's higher leverage amplifies cascade speed

#### 2. Liquidation Cascade (OI Drop Proxy)

Proxied by sudden OI drop. When OI decreases rapidly without corresponding price recovery, it indicates forced liquidations — margin calls cascading through the system. Hyperliquid's up-to-50x leverage makes cascades more violent than on Drift.

- Measures percentage OI drop over rolling 1-hour window
- Thresholds: 4% (LOW), 12% (HIGH), 25% (CRITICAL)

**Note:** This proxy is supplemented by the Real Liquidation Detector (see below) which tracks actual liquidation events.

#### 3. Funding Rate Volatility

Unstable funding rates signal regime transitions. When funding whipsaws between positive and negative, directional strategies face increased uncertainty. Hyperliquid settles funding hourly (vs continuous on Drift), making rate volatility more discrete.

- Rolling 24-entry standard deviation, annualized to bps
- Thresholds: 500 bps (LOW), 1500 bps (HIGH), 3000 bps (CRITICAL)

#### 4. Spread Blow-out

Mark/oracle divergence across markets indicates thin liquidity, forced selling, or price manipulation. Large spreads precede costly rebalances.

- Max absolute mark/oracle spread across monitored markets
- Thresholds: 0.3% (LOW), 1.0% (HIGH), 2.5% (CRITICAL)
- Tighter than Yogi (0.5/1.5/3.0%) because Hyperliquid's orderbook should maintain tighter spreads in normal conditions

### Hyperliquid-Specific Signal Enhancements

Beyond the four base signal dimensions (shared with Yogi), Kodiak adds three Hyperliquid-native intelligence modules:

#### 5. Real Liquidation Detector

**Replaces OI-drop proxy with actual liquidation event tracking.**

On Hyperliquid, liquidation trades carry a zero-hash signature (`0x000...000`). By filtering recent trades for this pattern, Kodiak detects real liquidation events with:

- **Volume tracking**: Total USD liquidated per market in a rolling 5-minute window
- **Intensity measurement**: USD/min liquidation rate, classified into severity levels:
  - BTC: $5K/min (LOW), $50K/min (HIGH), $200K/min (CRITICAL)
  - ETH: $3K/min (LOW), $30K/min (HIGH), $150K/min (CRITICAL)
  - SOL: $1K/min (LOW), $10K/min (HIGH), $50K/min (CRITICAL)
  - HYPE: $2K/min (LOW), $20K/min (HIGH), $100K/min (CRITICAL)
- **Direction bias**: Whether longs or shorts are being squeezed (from trade side)
- **Cascade detection**: When intensity increases >50% between detection cycles AND exceeds $1K/min, escalates to CRITICAL regardless of threshold

The liquidation severity is combined with the base signal severity (max of both). A cascade detection automatically escalates to CRITICAL.

**First live result (2026-03-20):** Detected a HYPE short squeeze — $18,264 liquidated in 5 minutes (9 events, $3,653/min). Correctly escalated to CRITICAL and switched regime to defensive mode (15% deployment, 0.2x leverage).

#### 6. Cross-Venue Funding Comparison

**Compares Hyperliquid's predicted funding rate against Binance and Bybit.**

Uses Hyperliquid's `predictedFundings` endpoint which provides next funding rates across venues. Each venue's rate is normalized to hourly for comparison:

- **HL funding >> CEX** (spread > 5% APY): Signal = `hl_high` — HL rate will likely converge down. SHORT is profitable now but faces convergence risk.
- **HL funding << CEX** (spread < -5% APY): Signal = `hl_low` — HL rate will likely converge up. LONG opportunity as HL catches up to CEX consensus.
- **All venues aligned**: Signal = `aligned` — Strong directional signal, higher confidence for entry.
- **No CEX data** (e.g., HYPE has no Binance/Bybit listing): No adjustment.

Cross-venue adjustment is applied during position entry, annotating the trade reason (e.g., "XV: HL funding +8.8% above CEX → SHORT profitable but convergence risk").

#### 7. Funding Pre-Positioning

**Times entries and exits to Hyperliquid's hourly funding settlement.**

Hyperliquid settles funding exactly on the hour (XX:00:00 UTC). The predicted rate is known before settlement. Kodiak uses a 10-minute pre-positioning window:

| Scenario | Current Position | Predicted Rate | Action |
|----------|-----------------|---------------|--------|
| 5 min to settlement | SHORT | Positive (+11% APY) | **Hold** — collecting funding |
| 5 min to settlement | LONG | Positive (+11% APY) | **Exit** — would pay funding |
| 5 min to settlement | SHORT | Negative (-15% APY) | **Exit** — would pay funding |
| 5 min to settlement | None | Positive (+11% APY) | **Enter SHORT** — capture settlement |
| 5 min to settlement | None | Low (+0.9% APY) | **Skip** — rate below 5% APY threshold |
| 20 min to settlement | Any | Any | **Wait** — outside pre-positioning window |

This runs every 30 seconds in the keeper loop, ensuring timely action near settlement.

### Regime Engine Decision Matrix

The regime engine combines vol regime (backward-looking) with signal severity (forward-looking, including liquidation and cross-venue data):

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
| Funding pre-positioning | Timed entries before hourly settlement (HL-specific) | 1-3% |
| Premium convergence | Mark/oracle mean reversion | 2-5% |
| Cross-venue arbitrage | Trade HL funding divergence from CEX consensus (HL-specific) | 1-2% |
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

1. **OI imbalance estimation** — Hyperliquid does not expose long/short OI split directly. We estimate imbalance from funding rate direction and magnitude. This is a proxy, not ground truth. The real liquidation detector partially compensates by providing actual direction bias data.
2. **No lending floor** — Unlike Yogi which earns 1.5-6.5% APY on idle capital via Kamino/Marginfi, Kodiak's idle USDC earns nothing. Capital efficiency depends entirely on perp deployment.
3. **Signal detection building track record** — The anomaly detector is adapted from Yogi's Drift-tuned thresholds. Hyperliquid's different microstructure may require threshold adjustments after live observation. Liquidation thresholds (USD/min) are initial estimates and may need calibration.
4. **Regime matrices are manually tuned** — The 5x4 deployment/leverage matrices were designed from first principles, not optimized from Hyperliquid historical data. Adaptive thresholds based on rolling volatility are planned for when capital scales beyond $5K.
5. **Single keeper SPOF** — A single Python process on EC2 handles all decisions. The dead man's switch (`scheduleCancel`) provides safety if the keeper goes offline, but multi-node redundancy is not yet implemented.
5. **Single-keeper architecture** — No multi-reporter consensus. The keeper is a single point of trust, mitigated by the dead man's switch.
6. **Unified account mode** — Hyperliquid's unified account mode shares collateral between spot and perp. Equity calculation must sum both to avoid false drawdown triggers.
7. **Cross-venue data availability** — Some coins (e.g., HYPE) have no Binance/Bybit listing, so cross-venue comparison is unavailable. The system falls back to HL-only signals for these markets.
8. **Liquidation detection via recentTrades** — The `recentTrades` endpoint returns only the most recent trades (typically 10-20). High-frequency liquidation events may be missed between 5-minute detection cycles. WebSocket streaming would improve coverage but adds infrastructure complexity.
9. **Pre-positioning assumes hourly settlement** — If Hyperliquid changes its funding interval, the pre-positioning logic would need adjustment.

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
+-- Every 5m:   Signal detection (4 base dimensions) + regime update
|               +-- Real liquidation detection (zero-hash trades) [HL-specific]
|               +-- Cross-venue funding comparison (HL vs CEX) [HL-specific]
|               +-- Combined severity = max(signals, liquidations, cascade)
|               --> Emergency rebalance if regime shifts dramatically
+-- Every 30m:  Funding scan + leverage update + imbalance scan
+-- Every 4h:   Full rebalance cycle
|   +-- Apply regime-adjusted deployment %
|   +-- Scale targets by regime-adjusted leverage
|   +-- Weight allocation by annualized funding rate
|   +-- Cross-venue adjustment on entry direction [HL-specific]
|   +-- Require 40%+ signal strength in cautious/defensive mode
|   +-- Close underperforming positions
|   +-- Open new positions in top 3 markets
+-- Every 30s:  Funding pre-positioning check [HL-specific]
|               +-- Evaluate predicted rate vs current positions
|               +-- Exit positions paying funding near settlement
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
| Liquidation detection | OI drop proxy (indirect) | Real liquidation events via zero-hash trades |
| Cross-venue comparison | Not available | HL vs Binance vs Bybit funding rates |
| Funding timing | No timing alpha (continuous) | Pre-positioning 10 min before hourly settlement |
| Signal dimensions | 4 | 6 (4 base + liquidation cascade + cross-venue) |
| Maker fees | -0.2 bps (rebate) | 1.5 bps |
| Lending floor | 30% to Kamino/Marginfi | None |
| Signal thresholds | OI shift 5/15/30%, spread 0.5/1.5/3.0% | OI shift 4/12/25%, spread 0.3/1.0/2.5% |
| Dead man's switch | Not native | Native scheduleCancel |
| Vault | Voltr (Ranger Earn) | Hyperliquid native |
