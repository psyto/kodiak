# Kodiak

**The biggest bear in the room. Hyperliquid funding rate vault with intelligent signal detection.**

Kodiak is a production-grade USDC vault that combines **delta-neutral funding rate harvesting** with a forward-looking anomaly detection engine on Hyperliquid. Instead of directional perp shorts, Kodiak opens paired spot + perp positions that cancel out price movement — collecting pure funding yield with near-zero price risk. Six signal dimensions drive a regime engine that adapts deployment before stress hits. Built on [Yogi](https://github.com/psyto/yogi) (Drift/Solana), extended with Hyperliquid-native intelligence.

## Strategy

Kodiak deploys capital into **delta-neutral positions** — buying spot and shorting perps on the same asset — to harvest funding rates with zero price exposure:

1. **Hybrid Execution — DN + Directional:**
   - **HYPE:** Tilted delta-neutral (spot buy + larger perp short, 10% short bias)
   - **BTC/ETH/SOL:** Directional shorts when funding is positive (capped at 20% of equity each)
   - Deploys more capital across more markets than pure DN alone
2. **Intelligence Layer** — Signal detection adjusts how much capital is deployed:
   - No anomalies: 70-100% deployed
   - Low signals: 55-85% deployed
   - Critical signals: 10-25% deployed
   - Extreme vol: 0% deployed, fully idle
3. **Auto-rotation** — Switches to higher-yielding assets when funding improves by >2x

### How It Works

```
USDC Deposit --> Hyperliquid Vault
                 |
                 +-- 100% --> Hybrid Engine (DN + Directional)
                              |
                              +-- HYPE: Tilted Delta-Neutral
                              |   +-- 70% capital --> Buy SPOT HYPE
                              |   +-- 30% capital --> Short PERP (10% larger)
                              |   +-- Price mostly hedged. Slight short bias.
                              |   +-- Slippage guard checks order book depth
                              |
                              +-- BTC/ETH/SOL: Directional Shorts
                              |   +-- When funding positive (>5% APY)
                              |   +-- Capped at 20% equity per position
                              |   +-- Managed by regime engine (close on CRITICAL)
                              |
                              +-- Signal Detector (every 5 min)
                              |   +-- OI imbalance shift (mass positioning)
                              |   +-- Funding rate volatility (regime transition)
                              |   +-- Spread blow-out (mark/oracle stress)
                              |   --> Severity: CLEAR / LOW / HIGH / CRITICAL
                              |
                              +-- Liquidation Detector (every 5 min) [HL-specific]
                              |   +-- Real liquidation events (zero-hash trades)
                              |   +-- Autocorrelation-aware (supersedes OI proxy)
                              |   --> Escalates to CRITICAL on cascade
                              |
                              +-- Cross-Venue Detector (every 5 min)
                              |   +-- HL vs Binance vs Bybit funding + OI
                              |   --> Entry direction adjustment
                              |
                              +-- Regime Engine (vol x signal --> deployment)
                              |   --> deploymentPct + rebalanceMode
                              |
                              +-- Funding Pre-Positioning [HL-specific]
                              |   +-- Pre-positions 10 min before settlement
                              |
                              +-- 30-second health monitoring
                              +-- Dead man's switch (auto-cancel if keeper offline)
```

### Tilted Delta-Neutral — Unique on Hyperliquid

No other Hyperliquid vault offers **configurable directional bias** within a delta-neutral framework. Liminal and Harmonix both run pure DN (delta=0). Kodiak adds an optional short tilt:

```
Pure DN:    spot=1.65 HYPE + perp=-1.65 HYPE  →  delta=0     (zero price risk)
Tilted DN:  spot=1.30 HYPE + perp=-1.43 HYPE  →  delta=-10%  (slight short bias)
```

**Why tilt?**

| Market | Pure DN | Tilted DN (10% short) |
|--------|--------|----------------------|
| Price drops 10% | $0 | **+profit** (tilt earns) |
| Price flat | Same | **+more funding** (larger perp) |
| Price pumps <11% | Same | Still better (funding covers tilt loss) |
| Price pumps >11% | Better | Tilt starts losing |

The tilt is configurable (`dn_tilt_pct` in config). Set to 0.0 for pure DN, 0.10 for 10% short bias. The regime engine can adjust this based on market conditions — more tilt in bearish markets, less in bullish.

**Break-even:** At HYPE +10.9% funding, the asset can pump 10.9% annually before the tilt loses money vs pure DN.

### What Makes Kodiak Different from Yogi

| | Yogi (Drift) | Kodiak (Hyperliquid) |
|---|---|---|
| Chain | Solana | Hyperliquid L1 |
| Language | TypeScript | Python |
| Vault | Voltr / Ranger Earn | Hyperliquid native vault |
| Delegation | Drift delegate model | Agent wallet |
| Funding | Continuous | Hourly settlement |
| Maker fees | -0.2 bps (rebate) | 1.5 bps |
| Max leverage | 2x | 2x (50x available, capped by regime) |
| Lending floor | 30% (Kamino/Marginfi) | Not integrated (HyperLend available on HyperEVM) |
| Markets | SOL, BTC, ETH, DOGE, SUI, AVAX | BTC, ETH, SOL, HYPE |
| Safety | scheduleCancel not native | Dead man's switch built-in |
| Execution | Directional perp shorts | **Tilted DN: spot buy + larger perp short** |
| Price risk | Directional (PnL swings) | **Near zero + configurable short bias** |
| Liquidation data | OI drop proxy | Real liquidation events (zero-hash trades) |
| Cross-venue | Drift vs Binance/Bybit | HL vs Binance vs Bybit funding + OI |
| Funding timing | Continuous (no timing alpha) | Hourly pre-positioning (10 min before settlement) |

### Yield Stack

| Source | Mechanism | Est. APY Contribution |
|--------|-----------|----------------------|
| Delta-neutral funding | Spot buy + perp short, collect hourly funding | 5-8% |
| Tilt directional income | 10% short bias profits when price drops | 0-5% (market dependent) |
| Tilt extra funding | Larger perp = 10% more funding collected | 0.5-1% |
| Funding pre-positioning | Enter before hourly settlement to capture known rates | 1-3% |
| Cross-venue timing | Trade when HL funding diverges from CEX consensus | 1-2% |
| **Combined target** | | **8-15% (bearish) / 5-8% (bullish)** |

**vs Pure DN:** Tilted DN earns more in bearish/flat markets (the common case). Only underperforms in strong bull markets where HYPE pumps >11% annually.

**vs Directional:** Tilted DN retains ~90% of the hedge while adding directional upside. Price swings are 10x smaller than pure directional.

## Architecture

### Components

| Module | File | Purpose |
|--------|------|---------|
| Delta-Neutral Engine | `src/keeper/delta_neutral.py` | Spot + perp paired positions, delta drift monitoring, auto-rotation |
| Signal Detector | `src/keeper/signal_detector.py` | 4-dimension anomaly detection (OI shift, funding vol, spread, OI drop proxy) |
| Liquidation Detector | `src/keeper/liquidation_detector.py` | **[HL-specific]** Real liquidation tracking via zero-hash trades, cascade detection |
| Cross-Venue Detector | `src/keeper/cross_venue_detector.py` | **[HL-specific]** HL vs Binance vs Bybit funding rate comparison |
| Funding Pre-Positioning | `src/keeper/funding_preposition.py` | **[HL-specific]** Pre-position before hourly funding settlement |
| Regime Engine | `src/keeper/regime_engine.py` | Vol x signal severity --> deployment % and leverage cap |
| Imbalance Detector | `src/keeper/imbalance_detector.py` | Reads premium, funding — computes composite signal and direction |
| Funding Scanner | `src/keeper/funding_scanner.py` | Fetches and ranks all Hyperliquid perp markets by funding rate |
| Cost Calculator | `src/keeper/cost_calculator.py` | Maker fee model — 5 bps round-trip cost |
| Leverage Controller | `src/keeper/leverage_controller.py` | Dynamic leverage scaling by vol regime (Parkinson estimator) |
| Health Monitor | `src/keeper/health_monitor.py` | 30-second margin ratio and drawdown checks |
| Position Manager | `src/keeper/position_manager.py` | Bidirectional position management with maker orders |
| Keeper Loop | `src/keeper/index.py` | Main event loop — signals, regime, imbalance, rebalance |
| Config | `src/config/` | Strategy parameters, signal thresholds, deployment matrices |

## Hyperliquid-Specific Intelligence

Three modules that leverage Hyperliquid-native data not available on other DEXes:

### 1. Real Liquidation Detection

Instead of proxying liquidations from OI drop (like Yogi on Drift), Kodiak detects **actual liquidation events** from Hyperliquid's trade data. Liquidation trades have a zero-hash signature, enabling:

- **Liquidation volume tracking** — USD amount liquidated per market per rolling window
- **Intensity measurement** — USD/min liquidation rate for severity classification
- **Direction bias** — whether longs or shorts are getting squeezed
- **Cascade detection** — accelerating liquidation rate (>100% increase and >$5k/min) triggers CRITICAL
- **Autocorrelation-aware** — when real liquidation data is available, the OI-drop proxy from the signal detector is excluded to prevent double-counting

First live result: Caught a HYPE short squeeze ($18,264 in 5 min, 9 events, $3,653/min) and correctly escalated to CRITICAL, switching the regime to defensive mode.

### 2. Cross-Venue Funding Comparison

Compares Hyperliquid's predicted funding rate against Binance and Bybit:

- **HL funding >> CEX** → HL rate will likely converge down (SHORT profitable but convergence risk)
- **HL funding << CEX** → HL rate will likely converge up (LONG opportunity)
- **All venues aligned** → High confidence in the directional signal

Entry decisions are annotated with cross-venue intelligence (e.g., "XV: HL funding +8.8% above CEX").

### 3. Funding Pre-Positioning

Hyperliquid settles funding hourly, on the hour. The predicted rate is known before settlement:

- **10-minute window** — Pre-position before each hourly settlement
- **Hold favorable positions** — Keep SHORT when positive funding, LONG when negative
- **Exit unfavorable positions** — Close positions that would pay funding at settlement
- **Skip low rates** — Don't bother positioning for rates below 5% APY

This is pure timing alpha — capturing a known event, not predicting.

## Regime Engine

The regime engine is Kodiak's core differentiator. It combines two inputs into a deployment decision:

### Deployment Matrix (Vol Regime x Signal Severity)

|  | CLEAR | LOW | HIGH | CRITICAL |
|--|-------|-----|------|----------|
| **Very Low** (< 20% vol) | 100% @ 2.0x | 80% @ 1.5x | 50% @ 1.0x | 25% @ 0.5x |
| **Low** (20-35%) | 85% @ 1.5x | 70% @ 1.2x | 40% @ 0.8x | 20% @ 0.3x |
| **Normal** (35-50%) | 70% @ 1.0x | 55% @ 0.8x | 30% @ 0.5x | 15% @ 0.2x |
| **High** (50-75%) | 50% @ 0.5x | 35% @ 0.3x | 20% @ 0.2x | 10% @ 0.0x |
| **Extreme** (> 75%) | 0% @ 0.0x | 0% @ 0.0x | 0% @ 0.0x | 0% @ 0.0x |

Key design principle: **signals can only reduce deployment, never increase it.** Extreme vol shuts down regardless. The intelligence layer catches danger *between* vol regime transitions.

### Signal Detection Thresholds (tuned for Hyperliquid)

| Dimension | LOW | HIGH | CRITICAL |
|-----------|-----|------|----------|
| OI Imbalance Shift | 4% in 1h | 12% | 25% |
| Liquidation Cascade (OI drop) | 4% in 1h | 12% | 25% |
| Funding Rate Volatility | 500 bps annualized | 1500 bps | 3000 bps |
| Spread Blow-out (mark/oracle) | 0.3% | 1.0% | 2.5% |

Thresholds are slightly tighter than Yogi's because Hyperliquid allows up to 50x leverage, creating more aggressive liquidation cascades.

## Execution Cost Gate

| | Taker | Maker (Kodiak) |
|---|---|---|
| Hyperliquid fee | 4.5 bps | 1.5 bps |
| Round-trip cost | 11 bps | 5 bps |
| Break-even (7-day hold) | 5.73% APY | 2.61% APY |

## Risk Management

| Parameter | Value |
|-----------|-------|
| Max drawdown | 3% reduce / 5% close all |
| Max leverage | 2x (regime-adaptive) |
| Health check | Every 30 seconds |
| Signal detection | Every 5 minutes |
| Margin ratio critical | Close all at 1.08 |
| Signal CRITICAL | Force reduce largest position |
| Max per market | 40% |
| Max markets | 3 (whitelist: BTC/ETH/SOL/HYPE) |
| Min hold | 7 days |
| Max rotations | 2 per week |
| Min signal strength | 20% composite (40% in cautious/defensive) |
| Emergency rebalance | Triggered on 30%+ deployment drop |
| Dead man's switch | Auto-cancel all orders if keeper offline 1 hour |

## Deployment

### Prerequisites

- Python 3.11+
- Hyperliquid account with USDC
- AWS EC2 instance (optional, for 24/7 operation)

### Setup

```bash
# 1. Clone and install
git clone https://github.com/psyto/kodiak.git
cd kodiak
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your private key and network setting

# 3. Bridge USDC to Hyperliquid
# Deposit USDC via app.hyperliquid.xyz or bridge from Arbitrum

# 4. Run keeper
python -m src.keeper.index          # Local
# Or with pm2 for production:
pm2 start .venv/bin/python3.11 --name kodiak-keeper -- -u -m src.keeper.index
pm2 save && pm2 startup
```

### Optional: Create Vault

```bash
# Create vault via Hyperliquid web UI (app.hyperliquid.xyz/vaults)
# Requires 100 USDC creation fee + 100 USDC minimum deposit
# Set HL_VAULT_ADDRESS in .env after creation

# Or set up agent wallet for delegation:
python -m src.scripts.setup_agent
```

### Mainnet Deployment

Kodiak is **live on Hyperliquid mainnet** with an automated keeper running 24/7 on AWS EC2.

```
Keeper (EC2, 24/7)
  |
  +-- Agent wallet signs on behalf of master/vault
  +-- Signal detection (5 min) — 4D anomaly + liquidation + cross-venue
  +-- Funding pre-positioning (30 sec) — near hourly settlement
  +-- Health monitoring (30 sec) — margin + drawdown + signal severity
  +-- Rebalance (4 hours) — with cross-venue entry intelligence
  +-- Dead man's switch (1 hour auto-cancel)
  +-- Heartbeat logging every 30 seconds
```

### Hyperliquid API Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `metaAndAssetCtxs` | Market data, funding rates, OI, mark/oracle prices |
| `candleSnapshot` | Hourly candles for Parkinson vol estimator |
| `fundingHistory` | Historical funding rates for volatility calculation |
| `predictedFundings` | Cross-venue funding comparison (HL vs Binance vs Bybit) + pre-positioning |
| `recentTrades` | Real liquidation detection (zero-hash trade filtering) |
| `clearinghouseState` | Account positions, margin, PnL |
| `spotClearinghouseState` | Spot USDC balance (unified account mode) |
| `openOrders` | Active order management |
| `scheduleCancel` | Dead man's switch |

## Tech Stack

- **Vault infrastructure**: [Hyperliquid native vaults](https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/vaults) — 10% profit share, 1-day depositor lockup
- **Trading**: [Hyperliquid](https://hyperliquid.xyz) — perpetual futures execution via agent wallet delegation
- **Keeper**: Python bot on AWS EC2 with pm2 process management
- **SDK**: [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) (official)
- **Signal detection**: 6-dimension anomaly detector (4 base + real liquidation + cross-venue), autocorrelation-aware
- **Vol computation**: Parkinson estimator on BTC hourly candles
- **Data feed**: Hyperliquid REST API + WebSocket

## Lineage

Kodiak started as a port of [Yogi](https://github.com/psyto/yogi) from Drift/Solana to Hyperliquid. The core strategy brain (regime engine, signal detector, imbalance scoring, cost calculator) shares the same logic. Kodiak then extends beyond Yogi with three Hyperliquid-native modules:

| Module | Yogi (Drift) | Kodiak (Hyperliquid) |
|---|---|---|
| Liquidation detection | OI drop proxy (indirect) | Real liquidation events via zero-hash trades |
| Cross-venue funding | Not available | HL vs Binance vs Bybit comparison |
| Funding timing | Continuous (no timing alpha) | Hourly pre-positioning (10 min window) |
| Signal dimensions | 4 | 6 (4 base + liquidation cascade + cross-venue) |

## License

MIT
