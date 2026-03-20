"""
Create a Hyperliquid vault for Kodiak.

Usage:
    python -m src.scripts.create_vault

Requirements:
    - HL_PRIVATE_KEY set in .env (master wallet with ≥200 USDC)
    - Network set via HL_NETWORK (default: testnet)

The vault creation:
    1. Costs 100 USDC (creation fee)
    2. Requires 100 USDC minimum initial deposit from leader
    3. Leader earns 10% profit share on depositor profits
    4. Leader must maintain ≥5% ownership at all times
"""

import os
import sys

from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from src.config.constants import HL_MAINNET_API, HL_TESTNET_API


def main():
    load_dotenv()

    private_key = os.getenv("HL_PRIVATE_KEY")
    if not private_key:
        print("Error: HL_PRIVATE_KEY not set in .env")
        sys.exit(1)

    network = os.getenv("HL_NETWORK", "testnet")
    base_url = HL_MAINNET_API if network == "mainnet" else HL_TESTNET_API

    account = Account.from_key(private_key)
    print(f"Wallet: {account.address}")
    print(f"Network: {network}")
    print(f"API: {base_url}")

    info = Info(base_url, skip_ws=True)
    exchange = Exchange(account, base_url)

    # Check balance
    user_state = info.user_state(account.address)
    balance = float(user_state.get("marginSummary", {}).get("accountValue", "0"))
    print(f"Balance: ${balance:.2f}")

    if balance < 200:
        print(f"Error: Need at least $200 USDC (100 creation fee + 100 min deposit). Have ${balance:.2f}")
        sys.exit(1)

    # Create vault
    print("\nCreating vault...")
    print("  Name: Kodiak")
    print("  Description: Hyperliquid basis trade alpha with intelligent signal detection")
    print("  Creation fee: 100 USDC")
    print("  Initial deposit: 100 USDC")
    print("  Profit share: 10% (default)")

    try:
        result = exchange.create_vault(
            name="Kodiak",
            description="Hyperliquid basis trade alpha with intelligent signal detection",
            initial_deposit_usd=100,
        )
        print(f"\nVault created successfully!")
        print(f"Result: {result}")
        print(f"\nAdd this to your .env:")
        print(f"HL_VAULT_ADDRESS={result.get('vault', 'check_explorer')}")
    except Exception as e:
        print(f"\nFailed to create vault: {e}")
        print("\nNote: The create_vault method may not be in the SDK yet.")
        print("You may need to create the vault via the Hyperliquid web UI:")
        print("  1. Go to https://app.hyperliquid.xyz/vaults")
        print("  2. Click 'Create Vault'")
        print("  3. Set name: Kodiak")
        print("  4. Deposit 100 USDC")
        print("  5. Copy the vault address to .env")
        sys.exit(1)


if __name__ == "__main__":
    main()
