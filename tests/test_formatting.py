"""Tests for number/address/message formatting."""

from __future__ import annotations

from decimal import Decimal

import formatting as fmt
from db import Wallet
from rpc import Fill, Position

WALLET = Wallet(
    address="0x1e1aabe8746cdf9166fe7c51bfc8e2438bc1714e",
    label="whale1",
    added_by=1,
    added_at=0,
    privacy_state="disabled",
    baselined=True,
)


def test_short_address_matches_spec_shape() -> None:
    assert fmt.short_address(WALLET.address) == "0x1e1a…714e"


def test_short_address_leaves_short_strings_alone() -> None:
    assert fmt.short_address("0xabc") == "0xabc"


def test_usd_two_decimals_with_separators() -> None:
    assert fmt.fmt_usd(Decimal("1079297.39986812")) == "$1,079,297.40"
    assert fmt.fmt_usd(Decimal("-33007.0198572252612")) == "-$33,007.02"
    assert fmt.fmt_usd(None) == "—"


def test_signed_usd_always_shows_sign() -> None:
    assert fmt.fmt_signed_usd(Decimal("500")) == "+$500.00"
    assert fmt.fmt_signed_usd(Decimal("-500")) == "-$500.00"


def test_sub_dollar_prices_keep_significant_digits() -> None:
    """A fixed 2dp would render this real AKEUSDT mark price as '0.00'."""
    assert fmt.fmt_price(Decimal("0.00066503")) == "0.00066503"
    assert fmt.fmt_price(Decimal("0.0007289203079")) == "0.0007289203"


def test_large_prices_round_to_cents() -> None:
    assert fmt.fmt_price(Decimal("74050.21351863")) == "74,050.21"


def test_mid_range_prices_keep_four_decimals() -> None:
    assert fmt.fmt_price(Decimal("12.3456789")) == "12.3457"


def test_price_of_none_and_zero() -> None:
    assert fmt.fmt_price(None) == "—"
    assert fmt.fmt_price(Decimal("0")) == "0"


def test_tokens_drop_trailing_zero_clutter() -> None:
    assert fmt.fmt_tokens(Decimal("1.3400")) == "1.34"
    assert fmt.fmt_tokens(Decimal("1525961628")) == "1,525,961,628"
    assert fmt.fmt_tokens(Decimal("0.00100000")) == "0.001"


def test_delta_borrows_precision_from_its_operands() -> None:
    """A delta shown beside its operands must not out-precise them."""
    text = fmt.format_balance_changed(
        WALLET, "USDT", Decimal("996222.26328264"), Decimal("995981.88342741")
    )
    assert "996,222.26 → 995,981.88 (-240.38)" in text


def test_delta_falls_back_when_reference_would_round_it_away() -> None:
    """A real change must never render as (+0.00)."""
    assert fmt.fmt_tokens(Decimal("0.0005"), reference=Decimal("999999")) == "0.0005"


def test_no_scientific_notation_leaks_through() -> None:
    """Decimal.normalize() would render this as 5E+4."""
    assert fmt.fmt_tokens(Decimal("50000.00")) == "50,000"
    assert "E" not in fmt.fmt_price(Decimal("0.00000001"))


def test_labels_are_html_escaped() -> None:
    """An unescaped label breaks delivery of every message mentioning it."""
    hostile = Wallet(
        address=WALLET.address,
        label="<b>pwn</b> & co",
        added_by=1,
        added_at=0,
        privacy_state="disabled",
        baselined=True,
    )
    title = fmt.wallet_title(hostile)

    assert "&lt;b&gt;pwn&lt;/b&gt;" in title
    assert "&amp; co" in title
    assert "<b>pwn</b>" not in title


def test_wallet_title_without_label_is_just_the_address() -> None:
    unlabelled = Wallet(
        address=WALLET.address,
        label=None,
        added_by=1,
        added_at=0,
        privacy_state="disabled",
        baselined=True,
    )
    assert fmt.wallet_title(unlabelled) == "<code>0x1e1a…714e</code>"


def test_fill_message_has_direction_and_notional() -> None:
    fill = Fill(
        symbol="BTCUSDT",
        side="BUY",
        price=Decimal("70000"),
        qty=Decimal("0.5"),
        time=1_784_132_166_980,
    )
    text = fmt.format_fill(WALLET, fill)

    assert fmt.LONG_EMOJI in text
    assert "BTCUSDT" in text
    assert "$35,000.00" in text
    assert "UTC" in text


def test_sell_fill_uses_short_emoji() -> None:
    fill = Fill(
        symbol="BTCUSDT", side="SELL", price=Decimal("70000"), qty=Decimal("0.5"), time=0
    )
    assert fmt.SHORT_EMOJI in fmt.format_fill(WALLET, fill)


def test_pnl_emoji_tracks_sign() -> None:
    assert fmt.pnl_emoji(Decimal("-1")) == fmt.LOSS_EMOJI
    assert fmt.pnl_emoji(Decimal("1")) == fmt.PROFIT_EMOJI


def test_resize_message_shows_old_to_new() -> None:
    old = Position(
        symbol="BTCUSDT",
        position_side="LONG",
        position_amount=Decimal("1"),
        notional_value=Decimal("50000"),
        unrealized_profit=Decimal("-100"),
        entry_price=Decimal("50000"),
        mark_price=Decimal("49900"),
    )
    new = Position(
        symbol="BTCUSDT",
        position_side="LONG",
        position_amount=Decimal("3"),
        notional_value=Decimal("150000"),
        unrealized_profit=Decimal("-300"),
        entry_price=Decimal("50000"),
        mark_price=Decimal("49900"),
    )
    text = fmt.format_position_resized(WALLET, old, new)

    assert "Increased" in text
    assert "1 → 3" in text
    assert "(+2)" in text
    assert fmt.LOSS_EMOJI in text


def test_shrinking_position_reads_as_decreased() -> None:
    big = Position(
        symbol="BTCUSDT",
        position_side="LONG",
        position_amount=Decimal("3"),
        notional_value=Decimal("150000"),
        unrealized_profit=Decimal("0"),
        entry_price=Decimal("50000"),
        mark_price=Decimal("50000"),
    )
    small = Position(
        symbol="BTCUSDT",
        position_side="LONG",
        position_amount=Decimal("1"),
        notional_value=Decimal("50000"),
        unrealized_profit=Decimal("0"),
        entry_price=Decimal("50000"),
        mark_price=Decimal("50000"),
    )
    text = fmt.format_position_resized(WALLET, big, small)

    assert "Decreased" in text
    assert "3 → 1" in text
    assert "(-2)" in text


def test_private_card_says_withheld_not_empty() -> None:
    text = fmt.format_wallet_card(WALLET, [], {}, None, is_private=True)

    assert fmt.PRIVATE_EMOJI in text
    assert "Private" in text
    assert "No open positions" not in text  # would be a lie: they're hidden


def test_wallet_card_shows_every_required_field() -> None:
    position = Position(
        symbol="AKEUSDT",
        position_side="LONG",
        position_amount=Decimal("1525961628"),
        notional_value=Decimal("1014810.26146884"),
        unrealized_profit=Decimal("-97494.1582565052612"),
        entry_price=Decimal("0.0007289203079"),
        mark_price=Decimal("0.00066503"),
    )
    text = fmt.format_wallet_card(
        WALLET, [position], {"USDT": Decimal("995981.88342741")}, None, is_private=False
    )

    assert "whale1" in text and "0x1e1a…714e" in text  # label + short address
    assert "995,981.88" in text                        # balance
    assert "AKEUSDT" in text and "LONG" in text        # symbol + side
    assert "1,525,961,628" in text                     # size in tokens
    assert "$1,014,810.26" in text                     # size in USD
    assert "0.0007289203" in text                      # entry
    assert "0.00066503" in text                        # mark
    assert "-$97,494.16" in text                       # PnL
    assert fmt.LOSS_EMOJI in text                      # PnL sign emoji


# ------------------------------------------------------------- chunking


def test_chunking_keeps_messages_under_the_cap() -> None:
    blocks = ["x" * 1000 for _ in range(20)]
    messages = fmt.chunk_messages(blocks)

    assert len(messages) > 1
    assert all(len(m) <= fmt.TELEGRAM_LIMIT for m in messages)


def test_chunking_keeps_small_lists_in_one_message() -> None:
    assert len(fmt.chunk_messages(["a", "b", "c"])) == 1


def test_oversized_single_block_is_split_not_dropped() -> None:
    blocks = ["\n".join("line" * 50 for _ in range(200))]
    messages = fmt.chunk_messages(blocks)

    assert all(len(m) <= fmt.TELEGRAM_LIMIT for m in messages)
    assert len(messages) > 1


def test_chunking_preserves_all_blocks() -> None:
    blocks = [f"block-{i}" for i in range(50)]
    joined = "\n\n".join(fmt.chunk_messages(blocks))

    for block in blocks:
        assert block in joined
