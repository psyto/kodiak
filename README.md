# Kodiak

**The biggest bear in the room. Hyperliquid funding rate vault with intelligent signal detection.**

Kodiak is a production-grade USDC vault that combines Hyperliquid perp funding rate arbitrage with a forward-looking anomaly detection engine. Four signal dimensions — OI imbalance shift, liquidation cascades, funding rate volatility, and spread blow-outs — drive a regime engine that adapts deployment and leverage before stress hits. Ported from [Yogi](https://github.com/psyto/yogi) (Drift/Solana) to Hyperliquid's native vault system.

## Strategy

Kodiak deploys capital into Hyperliquid perp positions to harvest funding rates, with an intelligence layer that dynamically adjusts exposure:

1. **Regime-Adaptive Arbitrage (100%)** — Three stacked yield sources:
   - **Funding rate** — Bidirectional: SHORT when positive, LONG when negative
   - **Premium convergence** — Mark/oracle deviation mean-reverts
   - **OI rebalancing** — Position ahead of funding rate changes using imbalance signals
2. **Intelligence Layer** — Signal detection adjusts how much capital is deployed:
   - No anomalies: 70-100% deployed at target leverage
   - Low signals: 55-85% deployed, reduced leverage
   - Critical signals: 10-25% deployed, minimal leverage
   - Extreme vol: 0% deployed, fully idle

### How It Works

```
USDC Deposit --> Hyperliquid Vault
                 |
                 +-- 100% --> Hyperliquid Perps (regime-adaptive arbitrage)
                              |
                              +-- Signal Detector (every 5 min)
                              |   +-- OI imbalance shift (mass positioning)
                              |   +-- Liquidation cascade (OI drop proxy)
                              |   +-- Funding rate volatility (regime transition)
                              |   +-- Spread blow-out (mark/oracle stress)
                              |   --> Severity: CLEAR / LOW / HIGH / CRITICAL
                              |
                              +-- Regime Engine (vol x signal --> deployment)
                              |   +-- Reads vol regime (Parkinson estimator)
                              |   +-- Reads signal severity (detector output)
                              |   --> deploymentPct + maxLeverage + rebalanceMode
                              |
                              +-- Imbalance Detector (premium + funding)
                              +-- Direction: SHORT or LONG based on composite signal
                              +-- Maker limit orders (1.5 bps fee)
                              +-- 30-second health monitoring
                              +-- Low turnover: 7-day min hold
                              +-- Dead man's switch (auto-cancel if keeper offline)
```

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
| Lending floor | 30% (Kamino/Marginfi) | None (100% to perps) |
| Markets | SOL, BTC, ETH, DOGE, SUI, AVAX | BTC, ETH, SOL, HYPE |
| Safety | scheduleCancel not native | Dead man's switch built-in |

### Yield Stack

| Source | Mechanism | Est. APY Contribution |
|--------|-----------|----------------------|
| Funding harvesting | Bidirectional perp positions collect hourly funding | 8-15% |
| Premium convergence | Mark/oracle deviation mean-reverts | 2-5% |
| Maker execution | Limit orders reduce cost vs taker | 0.5-1% |
| **Combined target** | | **15-25% (normal) / 8-12% (hostile)** |

## Architecture

### Components

| Module | File | Purpose |
|--------|------|---------|
| Signal Detector | `src/keeper/signal_detector.py` | 4-dimension anomaly detection (OI shift, liquidation, funding vol, spread) |
| Regime Engine | `src/keeper/regime_engine.py` | Vol x signal severity --> deployment % and leverage cap |
| Imbalance Detector | `src/keeper/imbalance_detector.py` | Reads premium, funding — computes composite signal and direction |
| Funding Scanner | `src/keeper/funding_scanner.py` | Fetches and ranks all Hyperliquid perp markets by funding rate |
| Cost Calculator | `src/keeper/cost_calculator.py` | Maker fee model — 5 bps round-trip cost |
| Leverage Controller | `src/keeper/leverage_controller.py` | Dynamic leverage scaling by vol regime (Parkinson estimator) |
| Health Monitor | `src/keeper/health_monitor.py` | 30-second margin ratio and drawdown checks |
| Position Manager | `src/keeper/position_manager.py` | Bidirectional position management with maker orders |
| Keeper Loop | `src/keeper/index.py` | Main event loop — signals, regime, imbalance, rebalance |
| Config | `src/config/` | Strategy parameters, signal thresholds, deployment matrices |

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
  +-- Signal detection (5 min) via Hyperliquid REST API
  +-- Health monitoring (30 sec)
  +-- Rebalance (4 hours)
  +-- Dead man's switch (1 hour auto-cancel)
  +-- Heartbeat logging every 30 seconds
```

### Hyperliquid API Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `metaAndAssetCtxs` | Market data, funding rates, OI, mark/oracle prices |
| `candleSnapshot` | Hourly candles for Parkinson vol estimator |
| `fundingHistory` | Historical funding rates for volatility calculation |
| `predictedFundings` | Cross-venue funding rate comparison |
| `clearinghouseState` | Account positions, margin, PnL |
| `spotClearinghouseState` | Spot USDC balance (unified account mode) |
| `openOrders` | Active order management |
| `scheduleCancel` | Dead man's switch |

## Tech Stack

- **Vault infrastructure**: [Hyperliquid native vaults](https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/vaults) — 10% profit share, 1-day depositor lockup
- **Trading**: [Hyperliquid](https://hyperliquid.xyz) — perpetual futures execution via agent wallet delegation
- **Keeper**: Python bot on AWS EC2 with pm2 process management
- **SDK**: [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) (official)
- **Signal detection**: 4-dimension anomaly detector with configurable thresholds
- **Vol computation**: Parkinson estimator on BTC hourly candles
- **Data feed**: Hyperliquid REST API + WebSocket

## Lineage

Kodiak is a port of [Yogi](https://github.com/psyto/yogi) from Drift/Solana to Hyperliquid. The strategy brain (regime engine, 4D signal detector, imbalance scoring, cost calculator) is identical in logic — adapted for Hyperliquid's API, fee structure, hourly funding settlement, and vault system.

## License

MIT
