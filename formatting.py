"""Telegram message and number formatting.

HTML parse mode throughout, chosen over MarkdownV2 because MarkdownV2 requires
escaping 18 characters in every text run — including ``.``, ``-`` and ``(``,
which appear constantly in prices and addresses — and one miss makes Telegram
reject the whole message at send time.

Everything interpolated into markup goes through :func:`esc`. Labels are
attacker-controlled in the sense that any admin can set them, and an unescaped
``<`` silently breaks delivery.

Number formatting is deliberately magnitude-aware: this bot shows both
``74050.21`` and ``0.00066503``, and any fixed decimal count renders one of them
useless.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from db import Wallet
from rpc import AccountSnapshot, Fill, OpenOrder, Position

TELEGRAM_LIMIT = 4096
# Leaves room for the chunk counter suffix appended to split messages.
_CHUNK_LIMIT = 3900

LONG_EMOJI = "🟢"
SHORT_EMOJI = "🔴"
PROFIT_EMOJI = "📈"
LOSS_EMOJI = "📉"
PRIVATE_EMOJI = "🔒"
BALANCE_EMOJI = "💰"
TRACK_EMOJI = "👀"
ORDERS_EMOJI = "📋"


def esc(value: object) -> str:
    """Escape a value for Telegram HTML."""
    return html.escape(str(value), quote=False)


def _strip_zeros(value: Decimal) -> str:
    """Render a Decimal in plain notation without trailing-zero clutter.

    ``format(d, "f")`` rather than ``normalize()``: normalize turns 50000 into
    ``5E+4``, which is not something to show a trader.
    """
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _grouped(value: Decimal, decimals: int) -> str:
    quantized = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = f"{quantized:,f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def fmt_usd(value: Decimal | None) -> str:
    """USD to 2dp with thousands separators; sign leads the currency symbol."""
    if value is None:
        return "—"
    quantized = abs(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    return f"{sign}${quantized:,.2f}"


def fmt_signed_usd(value: Decimal | None) -> str:
    """USD with an explicit sign, for deltas and PnL."""
    if value is None:
        return "—"
    quantized = abs(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else "+"
    return f"{sign}${quantized:,.2f}"


def fmt_price(value: Decimal | None) -> str:
    """Format a price, keeping ~7 significant digits for sub-dollar assets.

    A fixed 2dp would render an AKEUSDT mark of 0.00066503 as ``0.00``.
    """
    if value is None:
        return "—"
    magnitude = abs(value)
    if magnitude == 0:
        return "0"
    if magnitude >= 1000:
        decimals = 2
    elif magnitude >= 1:
        decimals = 4
    else:
        # adjusted() is floor(log10(x)); more decimals the smaller the price.
        decimals = min(12, 6 - magnitude.adjusted())
    return _grouped(value, decimals)


def _token_decimals(magnitude: Decimal) -> int:
    if magnitude >= 1000:
        return 2
    if magnitude >= 1:
        return 4
    return 8


def fmt_tokens(value: Decimal | None, reference: Decimal | None = None) -> str:
    """Format a token quantity without trailing-zero noise.

    ``reference`` borrows the precision of another value, so a delta renders at
    the same precision as the numbers it came from: without it,
    ``996,222.26 → 995,981.88 (-240.3799)`` shows a delta more precise than
    either operand. Falls back to the value's own precision when the reference
    would round a real change down to zero.
    """
    if value is None:
        return "—"

    basis = abs(reference) if reference is not None else abs(value)
    text = _grouped(value, _token_decimals(basis))

    if reference is not None and value != 0 and Decimal(text.replace(",", "")) == 0:
        text = _grouped(value, _token_decimals(abs(value)))
    return text


def short_address(address: str) -> str:
    """``0x1e1aabe8…bc1714e`` -> ``0x1e1a…714e``."""
    if len(address) <= 12:
        return address
    return f"{address[:6]}…{address[-4:]}"


def wallet_title(wallet: Wallet) -> str:
    """Bold label (when set) plus the shortened address, both escaped."""
    short = f"<code>{esc(short_address(wallet.address))}</code>"
    if wallet.label:
        return f"<b>{esc(wallet.label)}</b> · {short}"
    return short


def fmt_time(epoch_ms: int) -> str:
    """Render a fill timestamp as UTC."""
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def direction_emoji(direction: str) -> str:
    return SHORT_EMOJI if direction == "SHORT" else LONG_EMOJI


def pnl_emoji(value: Decimal) -> str:
    return LOSS_EMOJI if value < 0 else PROFIT_EMOJI


def fmt_pnl(value: Decimal) -> str:
    return f"{pnl_emoji(value)} {fmt_signed_usd(value)}"


# ------------------------------------------------------------- event bodies


def format_fill(wallet: Wallet, fill: Fill) -> str:
    emoji = LONG_EMOJI if fill.side == "BUY" else SHORT_EMOJI
    notional = fill.price * fill.qty
    return (
        f"{emoji} <b>Fill · {esc(fill.side)}</b> {esc(fill.symbol)}\n"
        f"{wallet_title(wallet)}\n"
        f"{fmt_tokens(fill.qty)} @ {fmt_price(fill.price)} · {fmt_usd(notional)}\n"
        f"<i>{esc(fmt_time(fill.time))}</i>"
    )


def format_position_opened(wallet: Wallet, position: Position) -> str:
    return (
        f"{direction_emoji(position.direction)} <b>Opened {esc(position.direction)}</b>"
        f" {esc(position.symbol)}\n"
        f"{wallet_title(wallet)}\n"
        f"size {fmt_tokens(position.size)} · {fmt_usd(position.notional_value)}\n"
        f"entry {fmt_price(position.entry_price)}"
    )


def format_position_closed(wallet: Wallet, position: Position) -> str:
    return (
        f"{SHORT_EMOJI} <b>Closed {esc(position.direction)}</b> {esc(position.symbol)}\n"
        f"{wallet_title(wallet)}\n"
        f"size was {fmt_tokens(position.size)}\n"
        f"last PnL {fmt_pnl(position.unrealized_profit)}"
    )


def format_position_resized(wallet: Wallet, old: Position, new: Position) -> str:
    increased = abs(new.position_amount) > abs(old.position_amount)
    verb = "Increased" if increased else "Decreased"
    arrow = "🔼" if increased else "🔽"
    delta = abs(new.position_amount) - abs(old.position_amount)
    return (
        f"{arrow} <b>{verb} {esc(new.direction)}</b> {esc(new.symbol)}\n"
        f"{wallet_title(wallet)}\n"
        f"size {fmt_tokens(old.size)} → {fmt_tokens(new.size)}"
        f" ({'+' if delta > 0 else ''}{fmt_tokens(delta, reference=new.size)})\n"
        f"{fmt_usd(new.notional_value)} · entry {fmt_price(new.entry_price)}\n"
        f"PnL {fmt_pnl(new.unrealized_profit)}"
    )


def format_balance_changed(
    wallet: Wallet, asset: str, old: Decimal, new: Decimal
) -> str:
    delta = new - old
    reference = max(abs(old), abs(new))
    return (
        f"{BALANCE_EMOJI} <b>Balance {esc(asset)}</b>\n"
        f"{wallet_title(wallet)}\n"
        f"{fmt_tokens(old)} → {fmt_tokens(new)}"
        f" ({'+' if delta > 0 else ''}{fmt_tokens(delta, reference=reference)})"
    )


def format_went_private(wallet: Wallet) -> str:
    return (
        f"{PRIVATE_EMOJI} <b>Wallet went private</b>\n"
        f"{wallet_title(wallet)}\n"
        f"<i>Balances and positions are now hidden. New activity may not be visible "
        f"until privacy is switched off.</i>"
    )


def format_now_tracking(wallet: Wallet, snapshot: AccountSnapshot) -> str:
    lines = [f"{TRACK_EMOJI} <b>Now tracking</b> {wallet_title(wallet)}"]

    if snapshot.balances:
        balances = " · ".join(
            f"{fmt_tokens(b.wallet_balance)} {esc(b.asset)}" for b in snapshot.balances
        )
        lines.append(f"{BALANCE_EMOJI} {balances}")

    if snapshot.positions:
        lines.append(f"<b>Current positions ({len(snapshot.positions)}):</b>")
        lines.extend(_position_lines(p) for p in snapshot.positions)
    else:
        lines.append("<i>No open positions.</i>")

    lines.append("<i>Existing state recorded as the baseline — no alerts for it.</i>")
    return "\n".join(lines)


def _position_lines(position: Position) -> str:
    return (
        f"{direction_emoji(position.direction)} <b>{esc(position.symbol)}</b>"
        f" {esc(position.direction)}\n"
        f"   size {fmt_tokens(position.size)} · {fmt_usd(position.notional_value)}\n"
        f"   entry {fmt_price(position.entry_price)} · mark {fmt_price(position.mark_price)}\n"
        f"   PnL {fmt_pnl(position.unrealized_profit)}"
    )


# ----------------------------------------------------------------- /list


def format_wallet_card(
    wallet: Wallet,
    snapshot_positions: list[Position],
    balances: dict[str, Decimal],
    open_orders: list[OpenOrder] | None,
    is_private: bool,
) -> str:
    """Render one wallet's block for /list."""
    if is_private:
        return (
            f"{PRIVATE_EMOJI} {wallet_title(wallet)}\n"
            f"<i>Private — Aster withholds this wallet's balances and positions.</i>"
        )

    lines = [f"{TRACK_EMOJI} {wallet_title(wallet)}"]

    if balances:
        rendered = " · ".join(
            f"{fmt_tokens(amount)} {esc(asset)}" for asset, amount in sorted(balances.items())
        )
        lines.append(f"{BALANCE_EMOJI} {rendered}")
    else:
        lines.append(f"{BALANCE_EMOJI} <i>no balances</i>")

    if snapshot_positions:
        for position in snapshot_positions:
            lines.append(_position_lines(position))
    else:
        lines.append("<i>No open positions.</i>")

    if open_orders:
        lines.append(f"{ORDERS_EMOJI} <b>Open orders ({len(open_orders)}):</b>")
        for order in open_orders[:10]:
            lines.append(
                f"   {esc(order.side)} {fmt_tokens(order.orig_qty)}"
                f" {esc(order.symbol)} @ {fmt_price(order.price)}"
                f" <i>({esc(order.type)})</i>"
            )
        if len(open_orders) > 10:
            lines.append(f"   <i>…and {len(open_orders) - 10} more</i>")

    return "\n".join(lines)


def chunk_messages(blocks: list[str], limit: int = _CHUNK_LIMIT) -> list[str]:
    """Pack blocks into messages under Telegram's 4096-character cap.

    A 50-wallet /list runs to tens of thousands of characters; without this
    Telegram rejects the send outright. Blocks are kept whole where possible, and
    an oversized single block is split on line boundaries rather than truncated.
    """
    messages: list[str] = []
    current = ""

    for block in blocks:
        if len(block) > limit:
            if current:
                messages.append(current)
                current = ""
            messages.extend(_split_block(block, limit))
            continue

        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            messages.append(current)
            current = block
        else:
            current = candidate

    if current:
        messages.append(current)
    return messages


def _split_block(block: str, limit: int) -> list[str]:
    """Split one oversized block on line boundaries."""
    parts: list[str] = []
    current = ""
    for line in block.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                parts.append(current)
            current = line[:limit]
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
