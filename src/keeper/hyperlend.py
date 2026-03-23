"""
HyperLend Integration — Layer 4 of Kodiak's yield stack.

Deposits idle USDC into HyperLend on HyperEVM to earn lending yield (~5% APY).
Withdraws automatically when capital is needed for DN positions or when
a directional opportunity arises (funding >15% APY on any market).

Flow:
  1. HyperCore → HyperEVM: spotSend USDC to system address
  2. HyperEVM: Approve + supply USDC to HyperLend Pool
  3. HyperEVM: Withdraw from HyperLend when needed
  4. HyperEVM → HyperCore: Approve + deposit back via CoreWriter
"""

import time
from dataclasses import dataclass
from typing import Optional

from web3 import Web3
from eth_account import Account

from hyperliquid.exchange import Exchange

from src.config.constants import HL_MAINNET_API


# Contract addresses (HyperEVM mainnet)
USDC_SYSTEM_ADDRESS = "0x2000000000000000000000000000000000000000"
NATIVE_USDC = Web3.to_checksum_address("0xb88339CB7199b77E23DB6E890353E22632Ba630f")
CORE_DEPOSIT_WALLET = Web3.to_checksum_address("0x6b9e773128f453f5c2c60935ee2de2cbc5390a24")
HYPERLEND_POOL = Web3.to_checksum_address("0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b")
HYPERLEND_HUSDC = Web3.to_checksum_address("0x744E4f26ee30213989216E1632D9BE3547C4885b")
SPOT_DEX = 4294967295  # Destination for HyperEVM → HyperCore

# HyperEVM RPC
HYPEREVM_RPC = "https://rpc.hyperliquid.xyz/evm"

# ABIs (minimal, only what we need)
ERC20_ABI = [
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

# HyperLend Pool ABI (Aave V3 fork — standard supply/withdraw)
POOL_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "onBehalfOf", "type": "address"}, {"name": "referralCode", "type": "uint16"}],
     "name": "supply", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "to", "type": "address"}],
     "name": "withdraw", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
]

CORE_DEPOSIT_ABI = [
    {"inputs": [{"name": "amount", "type": "uint256"}, {"name": "destinationDex", "type": "uint32"}],
     "name": "deposit", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]


@dataclass
class LendingState:
    deposited_usdc: float       # Amount deposited in HyperLend
    earned_interest: float      # Estimated interest earned
    timestamp: float


def _get_w3(private_key: str) -> tuple:
    """Initialize Web3 and wallet for HyperEVM."""
    w3 = Web3(Web3.HTTPProvider(HYPEREVM_RPC))
    wallet = Account.from_key(private_key)
    address = Web3.to_checksum_address(wallet.address)
    return w3, wallet, address


def _send_tx(w3: Web3, wallet, tx: dict, private_key: str) -> str:
    """Sign and send a transaction, return tx hash."""
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    return tx_hash.hex()


def get_hyperevm_usdc_balance(private_key: str) -> float:
    """Get USDC balance on HyperEVM."""
    w3, _, address = _get_w3(private_key)
    usdc = w3.eth.contract(address=NATIVE_USDC, abi=ERC20_ABI)
    balance = usdc.functions.balanceOf(address).call()
    return balance / 1e6


def get_hyperlend_balance(private_key: str) -> float:
    """Get hUSDC (deposited USDC) balance on HyperLend."""
    w3, _, address = _get_w3(private_key)
    husdc = w3.eth.contract(address=HYPERLEND_HUSDC, abi=ERC20_ABI)
    balance = husdc.functions.balanceOf(address).call()
    return balance / 1e6


def transfer_to_hyperevm(exchange: Exchange, amount: float) -> bool:
    """Transfer USDC from HyperCore spot to HyperEVM."""
    try:
        result = exchange.spot_transfer(
            amount=amount,
            destination=USDC_SYSTEM_ADDRESS,
            token="USDC",
        )
        success = result.get("status") == "ok"
        print(f"HyperCore → HyperEVM: ${amount:.2f} USDC | {'OK' if success else 'FAILED'}")
        if success:
            time.sleep(3)  # Wait for credit on HyperEVM
        return success
    except Exception as e:
        print(f"Transfer to HyperEVM failed: {e}")
        return False


def transfer_to_hypercore(private_key: str, amount: float) -> bool:
    """Transfer USDC from HyperEVM back to HyperCore spot."""
    try:
        w3, wallet, address = _get_w3(private_key)
        amount_wei = int(amount * 1e6)
        nonce = w3.eth.get_transaction_count(address)

        # Step 1: Approve CoreDepositWallet
        usdc = w3.eth.contract(address=NATIVE_USDC, abi=ERC20_ABI)
        approve_tx = usdc.functions.approve(CORE_DEPOSIT_WALLET, amount_wei).build_transaction({
            "from": address,
            "nonce": nonce,
            "gas": 100000,
            "maxFeePerGas": w3.to_wei(0.1, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(0.05, "gwei"),
        })
        _send_tx(w3, wallet, approve_tx, private_key)

        # Step 2: Deposit to CoreWriter
        core = w3.eth.contract(address=CORE_DEPOSIT_WALLET, abi=CORE_DEPOSIT_ABI)
        deposit_tx = core.functions.deposit(amount_wei, SPOT_DEX).build_transaction({
            "from": address,
            "nonce": nonce + 1,
            "gas": 200000,
            "maxFeePerGas": w3.to_wei(0.1, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(0.05, "gwei"),
        })
        _send_tx(w3, wallet, deposit_tx, private_key)

        print(f"HyperEVM → HyperCore: ${amount:.2f} USDC | OK")
        time.sleep(3)
        return True
    except Exception as e:
        print(f"Transfer to HyperCore failed: {e}")
        return False


def deposit_to_hyperlend(private_key: str, amount: float) -> bool:
    """Deposit USDC into HyperLend Pool."""
    try:
        w3, wallet, address = _get_w3(private_key)
        amount_wei = int(amount * 1e6)
        nonce = w3.eth.get_transaction_count(address)

        # Step 1: Approve Pool
        usdc = w3.eth.contract(address=NATIVE_USDC, abi=ERC20_ABI)
        approve_tx = usdc.functions.approve(HYPERLEND_POOL, amount_wei).build_transaction({
            "from": address,
            "nonce": nonce,
            "gas": 100000,
            "maxFeePerGas": w3.to_wei(0.1, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(0.05, "gwei"),
        })
        _send_tx(w3, wallet, approve_tx, private_key)

        # Step 2: Supply to Pool
        pool = w3.eth.contract(address=HYPERLEND_POOL, abi=POOL_ABI)
        supply_tx = pool.functions.supply(NATIVE_USDC, amount_wei, address, 0).build_transaction({
            "from": address,
            "nonce": nonce + 1,
            "gas": 300000,
            "maxFeePerGas": w3.to_wei(0.1, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(0.05, "gwei"),
        })
        _send_tx(w3, wallet, supply_tx, private_key)

        print(f"HyperLend deposit: ${amount:.2f} USDC | OK")
        return True
    except Exception as e:
        print(f"HyperLend deposit failed: {e}")
        return False


def withdraw_from_hyperlend(private_key: str, amount: float = None) -> float:
    """
    Withdraw USDC from HyperLend Pool.
    If amount is None, withdraws all.
    Returns amount withdrawn.
    """
    try:
        w3, wallet, address = _get_w3(private_key)

        if amount is None:
            # Withdraw all — use max uint256
            amount_wei = 2**256 - 1
        else:
            amount_wei = int(amount * 1e6)

        nonce = w3.eth.get_transaction_count(address)

        pool = w3.eth.contract(address=HYPERLEND_POOL, abi=POOL_ABI)
        withdraw_tx = pool.functions.withdraw(NATIVE_USDC, amount_wei, address).build_transaction({
            "from": address,
            "nonce": nonce,
            "gas": 300000,
            "maxFeePerGas": w3.to_wei(0.1, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(0.05, "gwei"),
        })
        _send_tx(w3, wallet, withdraw_tx, private_key)

        # Check actual balance after withdrawal
        withdrawn = get_hyperevm_usdc_balance(private_key)
        print(f"HyperLend withdraw: ${withdrawn:.2f} USDC available | OK")
        return withdrawn
    except Exception as e:
        print(f"HyperLend withdraw failed: {e}")
        return 0.0


async def deposit_idle_usdc(
    exchange: Exchange,
    private_key: str,
    idle_usdc: float,
    min_deposit: float = 10.0,
) -> Optional[LendingState]:
    """
    Deposit idle USDC into HyperLend.
    Full flow: HyperCore → HyperEVM → HyperLend Pool
    """
    if idle_usdc < min_deposit:
        return None

    print(f"\n--- HyperLend Deposit: ${idle_usdc:.2f} ---")

    # Step 1: Transfer to HyperEVM
    if not transfer_to_hyperevm(exchange, idle_usdc):
        return None

    # Step 2: Deposit to HyperLend
    if not deposit_to_hyperlend(private_key, idle_usdc):
        # Try to send back to HyperCore
        transfer_to_hypercore(private_key, idle_usdc)
        return None

    return LendingState(
        deposited_usdc=idle_usdc,
        earned_interest=0.0,
        timestamp=time.time(),
    )


async def withdraw_all_to_hypercore(
    private_key: str,
) -> float:
    """
    Withdraw all USDC from HyperLend back to HyperCore.
    Full flow: HyperLend → HyperEVM → HyperCore
    Returns amount returned to HyperCore.
    """
    print("\n--- HyperLend Withdrawal ---")

    # Step 1: Withdraw from HyperLend
    available = withdraw_from_hyperlend(private_key)
    if available <= 0:
        print("Nothing to withdraw from HyperLend")
        return 0.0

    # Step 2: Transfer back to HyperCore
    if transfer_to_hypercore(private_key, available):
        return available
    return 0.0
