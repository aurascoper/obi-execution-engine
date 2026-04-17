"""
fund_perp.py — One-shot internal spot→perp USDC transfer.

Manually runnable (not imported, not wired into any engine). Moves USDC from
the Hyperliquid spot clearinghouse into the perp clearinghouse so the perp
ledger has margin available for update_leverage() and order submission.

Usage:
    python3 fund_perp.py

Requires:
    HL_WALLET_ADDRESS and HL_PRIVATE_KEY in .env.
    The signing key MUST be the master wallet — agent keys cannot perform
    USD class transfers on Hyperliquid.
"""

import asyncio
import os

from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

load_dotenv()


async def main() -> None:
    wallet_address = os.getenv("HL_WALLET_ADDRESS")
    private_key = os.getenv("HL_PRIVATE_KEY")

    if not wallet_address or not private_key:
        print("ERROR: HL_WALLET_ADDRESS or HL_PRIVATE_KEY not found in .env")
        return

    account = Account.from_key(private_key)
    exchange = Exchange(
        account,
        constants.MAINNET_API_URL,
        account_address=wallet_address,
    )

    transfer_amount = 474.0

    print(f"Initiating internal transfer of ${transfer_amount} spot→perp...")

    try:
        result = exchange.usd_class_transfer(transfer_amount, to_perp=True)

        if result.get("status") == "ok":
            print(f"SUCCESS: ${transfer_amount} moved to the Perp ledger.")
            print(
                "Next: re-run the balance check; accountValue should reflect the transfer."
            )
        else:
            print(f"TRANSFER FAILED: {result}")

    except Exception as exc:
        print(f"ERROR: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
