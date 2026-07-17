"""Telegram command handlers and the admin guard.

/list renders positions from the stored snapshot rather than re-fetching them.
The snapshot is never staler than one poll cycle, and re-fetching balances for
every wallet on demand would let anyone in the chat burn the shared rate budget
— the same budget the poller needs to not get the host IP banned.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import formatting as fmt
from config import Config
from db import Database, normalize_address
from rpc import AsterError, AsterRpcClient, OpenOrder

logger = logging.getLogger(__name__)

EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Open orders cost one live call per wallet. Past this many wallets /list skips
# them rather than making the caller wait on a serialised rate-limited queue.
OPEN_ORDERS_WALLET_LIMIT = 10

HELP_TEXT = (
    "<b>AsterDEX wallet tracker</b>\n"
    "I watch AsterDEX perpetuals wallets and post here when something changes: "
    "new fills, positions opened/closed/resized, and balance moves.\n\n"
    "<b>Commands</b>\n"
    "/list — show tracked wallets, balances and open positions\n"
    "/list &lt;address|label&gt; — detail for one wallet, including open orders\n"
    "/add &lt;address&gt; [label] — track a wallet <i>(admins)</i>\n"
    "/remove &lt;address|label&gt; — stop tracking <i>(admins)</i>\n"
    "/help — this message\n\n"
    "<i>Entry and mark prices are derived from Aster's reported notional and PnL, "
    "which the API returns instead of the prices themselves.</i>"
)


def admin_only(func: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """Refuse the command politely unless the caller is a configured admin."""

    @wraps(func)
    async def wrapper(self: Handlers, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        message = update.effective_message
        if user is None or message is None:
            return
        if user.id not in self._config.admin_user_ids:
            await message.reply_text(
                "Sorry — only admins can change the watchlist. "
                "You can still use /list.",
            )
            logger.info("refused %s from non-admin %s", func.__name__, user.id)
            return
        await func(self, update, context)

    return wrapper


class Handlers:
    """Command handlers bound to the database, RPC client and config."""

    def __init__(self, config: Config, db: Database, rpc: AsterRpcClient) -> None:
        self._config = config
        self._db = db
        self._rpc = rpc

    async def start(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        """/start and /help — open to anyone."""
        message = update.effective_message
        if message:
            await message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)

    @admin_only
    async def add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/add <address> [label] — admins only."""
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return

        args = context.args or []
        if not args:
            await message.reply_text(
                "Usage: <code>/add &lt;address&gt; [label]</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        raw_address = args[0]
        # Aster accepts junk addresses and answers HTTP 200, so this regex is the
        # only thing standing between a typo and a permanently silent watchlist entry.
        if not EVM_ADDRESS_RE.match(raw_address):
            await message.reply_text(
                "That doesn't look like an EVM address. "
                "Expected 0x followed by 40 hex characters.",
            )
            return

        address = normalize_address(raw_address)
        label = " ".join(args[1:]).strip() or None

        if label and await self._db.get_wallet(label) is not None:
            await message.reply_text(f"The label {label!r} is already in use.")
            return

        added = await self._db.add_wallet(address, label, user.id)
        if not added:
            existing = await self._db.get_wallet(address)
            name = f" ({existing.label})" if existing and existing.label else ""
            await message.reply_text(f"Already tracking {fmt.short_address(address)}{name}.")
            return

        await message.reply_text(
            f"✅ Now tracking {fmt.wallet_title(await self._db.get_wallet(address))}\n"  # type: ignore[arg-type]
            f"<i>Current state will be recorded as the baseline on the next poll, "
            f"so you won't get alerts for positions it already holds.</i>",
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/remove <address|label> — admins only."""
        message = update.effective_message
        if message is None:
            return

        args = context.args or []
        if not args:
            await message.reply_text(
                "Usage: <code>/remove &lt;address|label&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        needle = " ".join(args).strip()
        removed = await self._db.remove_wallet(needle)
        if removed is None:
            await message.reply_text(f"Not tracking {fmt.esc(needle)} — nothing removed.")
            return

        await message.reply_text(f"🗑 Stopped tracking <code>{fmt.esc(removed)}</code>.", parse_mode=ParseMode.HTML)

    async def list_wallets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/list [address|label] — open to anyone."""
        message = update.effective_message
        if message is None:
            return

        args = context.args or []
        if args:
            needle = " ".join(args).strip()
            wallet = await self._db.get_wallet(needle)
            if wallet is None:
                await message.reply_text(f"Not tracking {fmt.esc(needle)}.")
                return
            wallets = [wallet]
        else:
            wallets = await self._db.list_wallets()

        if not wallets:
            await message.reply_text(
                "No wallets tracked yet. An admin can add one with "
                "<code>/add &lt;address&gt; [label]</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        with_orders = len(wallets) <= OPEN_ORDERS_WALLET_LIMIT
        blocks: list[str] = []

        for wallet in wallets:
            if not wallet.baselined:
                blocks.append(
                    f"{fmt.TRACK_EMOJI} {fmt.wallet_title(wallet)}\n"
                    f"<i>Awaiting first poll…</i>"
                )
                continue

            is_private = wallet.privacy_state == "enabled"
            positions = list((await self._db.get_positions(wallet.address)).values())
            positions.sort(key=lambda p: p.symbol)
            balances = await self._db.get_balances(wallet.address)

            orders: list[OpenOrder] | None = None
            if with_orders and not is_private:
                try:
                    orders = list(await self._rpc.open_orders(wallet.address))
                except AsterError as exc:
                    logger.warning("open orders unavailable for %s: %s", wallet.address, exc)

            blocks.append(
                fmt.format_wallet_card(wallet, positions, balances, orders, is_private)
            )

        if not with_orders:
            blocks.append(
                f"<i>Open orders omitted — {len(wallets)} wallets would take too many "
                f"API calls. Use /list &lt;label&gt; for one wallet's orders.</i>"
            )

        for chunk in fmt.chunk_messages(blocks):
            await message.reply_text(
                chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log handler exceptions instead of letting them surface to the user."""
    logger.exception("error handling update %s", update, exc_info=context.error)
