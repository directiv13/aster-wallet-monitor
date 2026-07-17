"""Read-only smoke test against the live Aster endpoint.

Exercises the RPC client and the /list rendering against real data without a
Telegram token, a database, or any writes. Useful for confirming Aster hasn't
changed its response shape.

    python smoke.py [address]

Deliberately makes only a handful of requests: Aster publishes no rate limit for
this endpoint and bans abusive IPs (HTTP 418), so this is not a load test and
must not be turned into one.
"""

from __future__ import annotations

import asyncio
import sys

import formatting as fmt
from config import DEFAULT_RPC_BASE_URL
from db import Wallet
from rpc import AsterRpcClient

SAMPLE_ADDRESS = "0x1e1aabe8746cdf9166fe7c51bfc8e2438bc1714e"


async def main(address: str) -> None:
    wallet = Wallet(
        address=address,
        label="smoke-test",
        added_by=None,
        added_at=0,
        privacy_state="disabled",
        baselined=True,
    )

    async with AsterRpcClient(DEFAULT_RPC_BASE_URL, max_requests_per_second=1.0) as rpc:
        snapshot = await rpc.get_balance(address)

        if snapshot.privacy:
            print(f"{address} reports accountPrivacy=enabled.")
            print("Balances and positions are withheld — note this is also what a")
            print("never-used wallet looks like; the two are indistinguishable.")
            return

        orders = list(await rpc.open_orders(address))
        fills = await rpc.user_fills(address)

    card = fmt.format_wallet_card(
        wallet,
        list(snapshot.positions),
        {b.asset: b.wallet_balance for b in snapshot.balances},
        orders,
        is_private=False,
    )

    print("--- /list card (HTML as Telegram receives it) ---")
    print(card)
    print()
    print(f"open orders: {len(orders)}")
    print(f"fills in the last 7 days: {len(fills)}")
    for fill in fills[:5]:
        print(f"  {fmt.format_fill(wallet, fill)}".replace("\n", " | "))

    print()
    print("--- price derivation check ---")
    for position in snapshot.positions:
        # entry is derived from pnl, so this round-trips it back out. Agreement
        # confirms the algebra; it cannot confirm Aster's own rounding.
        recomputed = (position.mark_price - position.entry_price) * position.position_amount
        print(f"  {position.symbol}: entry={position.entry_price} mark={position.mark_price}")
        print(f"    reported pnl={position.unrealized_profit}")
        print(f"    recomputed  ={recomputed}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else SAMPLE_ADDRESS
    asyncio.run(main(target))
