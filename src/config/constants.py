"""
Hyperliquid constants and API endpoints for Kodiak.
"""

# API endpoints
HL_MAINNET_API = "https://api.hyperliquid.xyz"
HL_TESTNET_API = "https://api.hyperliquid-testnet.xyz"
HL_MAINNET_WS = "wss://api.hyperliquid.xyz/ws"
HL_TESTNET_WS = "wss://api.hyperliquid-testnet.xyz/ws"

# Perp market mapping (coin name -> index)
# Hyperliquid uses coin names directly, but we track indices for consistency
HL_PERP_MARKETS = {
    "BTC": {"index": 0, "name": "BTC"},
    "ETH": {"index": 1, "name": "ETH"},
    "SOL": {"index": 5, "name": "SOL"},
    "HYPE": {"index": 132, "name": "HYPE"},
}

# Precision
PRICE_DECIMALS = 8  # Hyperliquid uses 8 decimal price precision
SIZE_DECIMALS = {
    "BTC": 4,   # 0.0001 BTC min
    "ETH": 3,   # 0.001 ETH min
    "SOL": 1,   # 0.1 SOL min
    "HYPE": 0,  # 1 HYPE min
}

# Fee tiers (base tier — no volume discount)
HL_TAKER_FEE_BPS = 4.5   # 0.045%
HL_MAKER_FEE_BPS = 1.5   # 0.015%

# Funding
FUNDING_SETTLE_INTERVAL_HOURS = 1  # Hourly settlement
FUNDING_RATE_CAP_PER_HOUR = 0.04   # 4% per hour cap
FUNDING_INTEREST_RATE_8H = 0.0001  # 0.01% per 8 hours (fixed)

# Vault
VAULT_CREATION_FEE_USDC = 100
VAULT_MIN_LEADER_DEPOSIT_USDC = 100
VAULT_LEADER_MIN_OWNERSHIP_PCT = 5
VAULT_PROFIT_SHARE_PCT = 10
VAULT_DEPOSITOR_LOCKUP_DAYS = 1
