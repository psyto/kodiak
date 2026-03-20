"""
Set up an agent wallet for Kodiak keeper.

Usage:
    python -m src.scripts.setup_agent

This creates an agent wallet that can sign trading operations
on behalf of the master wallet (or vault), without holding
the master private key.

Equivalent to Drift's delegate model.
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

    master_account = Account.from_key(private_key)
    exchange = Exchange(master_account, base_url)

    print(f"Master wallet: {master_account.address}")
    print(f"Network: {network}")

    # Generate a new agent wallet
    agent_account = Account.create()
    agent_address = agent_account.address
    agent_private_key = agent_account.key.hex()

    print(f"\nGenerated agent wallet: {agent_address}")

    # Approve the agent wallet
    print("Approving agent wallet...")
    try:
        result = exchange.approve_agent(agent_address)
        print(f"Agent approved: {result}")

        print(f"\nAdd this to your .env:")
        print(f"HL_AGENT_PRIVATE_KEY={agent_private_key}")
        print(f"\nIMPORTANT: Save the agent private key securely!")
        print(f"The agent can sign trades on behalf of your master wallet.")
    except Exception as e:
        print(f"Failed to approve agent: {e}")
        print("\nYou may need to approve the agent manually via the API.")
        print(f"Agent address: {agent_address}")
        print(f"Agent private key: {agent_private_key}")
        sys.exit(1)


if __name__ == "__main__":
    main()
