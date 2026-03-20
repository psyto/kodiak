"""
Transfer USDC from HyperEVM (spot) to HyperCore (perp margin).

Run after depositing USDC to HyperEVM via Backpack or bridge.

Usage:
    python -m src.scripts.transfer_to_perp [amount]

Example:
    python -m src.scripts.transfer_to_perp 500
"""

import os
import sys

from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants as hl_constants

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
    exchange = Exchange(account, base_url)

    print(f"Wallet: {account.address}")
    print(f"Network: {network}")

    # Get amount from command line
    if len(sys.argv) < 2:
        print("Usage: python -m src.scripts.transfer_to_perp <amount>")
        print("Example: python -m src.scripts.transfer_to_perp 500")
        sys.exit(1)

    amount = float(sys.argv[1])
    print(f"Transferring ${amount:.2f} USDC from spot (HyperEVM) to perp (HyperCore)...")

    try:
        result = exchange.usd_class_transfer(amount, to_perp=True)
        print(f"Transfer result: {result}")
        print(f"${amount:.2f} USDC is now available as perp margin.")
    except Exception as e:
        print(f"Transfer failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
