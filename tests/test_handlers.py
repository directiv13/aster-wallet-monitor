"""Tests for command handlers, the admin guard, and config validation."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from config import ConfigError, load_config
from db import Database
from handlers import EVM_ADDRESS_RE, Handlers

ADDRESS = "0x1e1aabe8746cdf9166fe7c51bfc8e2438bc1714e"
ADMIN_ID = 111
CIVILIAN_ID = 222


class FakeMessage:
    """Captures replies instead of calling Telegram."""

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_kwargs) -> None:
        self.replies.append(text)

    @property
    def last(self) -> str:
        return self.replies[-1]


def make_update(user_id: int) -> tuple[SimpleNamespace, FakeMessage]:
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
    )
    return update, message


def make_context(args: list[str]) -> SimpleNamespace:
    return SimpleNamespace(args=args)


@pytest.fixture
async def handlers(tmp_path):
    from tests.test_poller import make_config

    config = make_config(tmp_path, admin_user_ids=frozenset({ADMIN_ID}))
    db = Database(config.db_path)
    await db.connect()
    handler = Handlers(config, db, rpc=None)  # type: ignore[arg-type]
    yield handler, db
    await db.close()


# ------------------------------------------------------- address validation


def test_address_regex_accepts_the_sample_wallet() -> None:
    assert EVM_ADDRESS_RE.match(ADDRESS)


def test_address_regex_accepts_mixed_case() -> None:
    assert EVM_ADDRESS_RE.match("0x1E1AABE8746CDF9166FE7C51BFC8E2438BC1714E")


@pytest.mark.parametrize(
    "bad",
    [
        "0xdeadbeef",                                      # too short, yet Aster 200s on it
        "1e1aabe8746cdf9166fe7c51bfc8e2438bc1714e",        # no 0x
        "0x1e1aabe8746cdf9166fe7c51bfc8e2438bc1714",       # 39 hex chars
        "0x1e1aabe8746cdf9166fe7c51bfc8e2438bc1714ef",     # 41 hex chars
        "0xzzzaabe8746cdf9166fe7c51bfc8e2438bc1714e",      # non-hex
        "",
    ],
)
def test_address_regex_rejects_junk(bad: str) -> None:
    assert not EVM_ADDRESS_RE.match(bad)


# -------------------------------------------------------------- admin guard


async def test_non_admin_cannot_add(handlers) -> None:
    """Acceptance #1: a non-admin is refused, politely, and nothing is stored."""
    handler, db = handlers
    update, message = make_update(CIVILIAN_ID)

    await handler.add(update, make_context([ADDRESS, "whale1"]))

    assert "admins" in message.last.lower()
    assert await db.list_wallets() == []


async def test_non_admin_cannot_remove(handlers) -> None:
    handler, db = handlers
    await db.add_wallet(ADDRESS, "whale1", ADMIN_ID)
    update, message = make_update(CIVILIAN_ID)

    await handler.remove(update, make_context(["whale1"]))

    assert "admins" in message.last.lower()
    assert len(await db.list_wallets()) == 1  # still tracked


async def test_non_admin_can_list(handlers) -> None:
    handler, _db = handlers
    update, message = make_update(CIVILIAN_ID)

    await handler.list_wallets(update, make_context([]))

    assert "admins" not in message.last.lower()


# ---------------------------------------------------------------- /add


async def test_admin_add_confirms_and_stores(handlers) -> None:
    """Acceptance #1: an admin adds and gets a confirmation."""
    handler, db = handlers
    update, message = make_update(ADMIN_ID)

    await handler.add(update, make_context([ADDRESS, "whale1"]))

    wallets = await db.list_wallets()
    assert len(wallets) == 1
    assert wallets[0].label == "whale1"
    assert wallets[0].added_by == ADMIN_ID
    assert "✅" in message.last


async def test_add_normalises_case(handlers) -> None:
    """Same wallet in different case must not become two rows."""
    handler, db = handlers
    update, _message = make_update(ADMIN_ID)

    await handler.add(update, make_context([ADDRESS.upper().replace("0X", "0x")]))
    wallets = await db.list_wallets()

    assert wallets[0].address == ADDRESS.lower()


async def test_add_rejects_invalid_address(handlers) -> None:
    handler, db = handlers
    update, message = make_update(ADMIN_ID)

    await handler.add(update, make_context(["0xdeadbeef"]))

    assert "EVM address" in message.last
    assert await db.list_wallets() == []


async def test_add_rejects_duplicate(handlers) -> None:
    handler, db = handlers
    update, message = make_update(ADMIN_ID)

    await handler.add(update, make_context([ADDRESS, "whale1"]))
    await handler.add(update, make_context([ADDRESS, "whale2"]))

    assert "already tracking" in message.last.lower()
    assert len(await db.list_wallets()) == 1


async def test_add_rejects_duplicate_label(handlers) -> None:
    """Labels are a /remove key, so they have to stay unique."""
    handler, db = handlers
    update, message = make_update(ADMIN_ID)

    await handler.add(update, make_context([ADDRESS, "whale1"]))
    await handler.add(update, make_context(["0x" + "a" * 40, "whale1"]))

    assert "already in use" in message.last
    assert len(await db.list_wallets()) == 1


async def test_add_accepts_multiword_label(handlers) -> None:
    handler, db = handlers
    update, _message = make_update(ADMIN_ID)

    await handler.add(update, make_context([ADDRESS, "big", "whale", "guy"]))

    assert (await db.list_wallets())[0].label == "big whale guy"


async def test_add_without_args_shows_usage(handlers) -> None:
    handler, _db = handlers
    update, message = make_update(ADMIN_ID)

    await handler.add(update, make_context([]))

    assert "/add" in message.last


# -------------------------------------------------------------- /remove


async def test_remove_by_address(handlers) -> None:
    handler, db = handlers
    update, message = make_update(ADMIN_ID)
    await db.add_wallet(ADDRESS, "whale1", ADMIN_ID)

    await handler.remove(update, make_context([ADDRESS]))

    assert await db.list_wallets() == []
    assert "🗑" in message.last


async def test_remove_by_label(handlers) -> None:
    handler, db = handlers
    update, _message = make_update(ADMIN_ID)
    await db.add_wallet(ADDRESS, "whale1", ADMIN_ID)

    await handler.remove(update, make_context(["whale1"]))

    assert await db.list_wallets() == []


async def test_remove_untracked_errors(handlers) -> None:
    handler, _db = handlers
    update, message = make_update(ADMIN_ID)

    await handler.remove(update, make_context(["nobody"]))

    assert "not tracking" in message.last.lower()


async def test_remove_purges_all_wallet_state(handlers) -> None:
    """Leftover rows would resurrect as stale diffs if the wallet is re-added."""
    handler, db = handlers
    update, _message = make_update(ADMIN_ID)
    await db.add_wallet(ADDRESS, "whale1", ADMIN_ID)
    await db.commit_cycle(
        ADDRESS,
        balances=[],
        positions=[],
        fill_counts={"abc": (1, 123)},
        last_fill_time=123,
        privacy_state="disabled",
    )

    await handler.remove(update, make_context(["whale1"]))

    assert await db.get_seen_fill_counts(ADDRESS) == {}
    assert await db.get_fill_cursor(ADDRESS) is None


# ---------------------------------------------------------------- /list


async def test_list_empty_watchlist(handlers) -> None:
    handler, _db = handlers
    update, message = make_update(CIVILIAN_ID)

    await handler.list_wallets(update, make_context([]))

    assert "No wallets tracked yet" in message.last


async def test_list_flags_private_wallets(handlers) -> None:
    """Acceptance #2: privacy-enabled wallets are flagged."""
    handler, db = handlers
    await db.add_wallet(ADDRESS, "whale1", ADMIN_ID)
    await db.commit_cycle(
        ADDRESS,
        balances=[],
        positions=[],
        fill_counts={},
        last_fill_time=None,
        privacy_state="enabled",
    )
    update, message = make_update(CIVILIAN_ID)

    await handler.list_wallets(update, make_context([ADDRESS]))

    assert "Private" in message.last


async def test_list_shows_awaiting_first_poll(handlers) -> None:
    handler, db = handlers
    await db.add_wallet(ADDRESS, "whale1", ADMIN_ID)
    update, message = make_update(CIVILIAN_ID)

    await handler.list_wallets(update, make_context([]))

    assert "Awaiting first poll" in message.last


async def test_list_unknown_wallet(handlers) -> None:
    handler, _db = handlers
    update, message = make_update(CIVILIAN_ID)

    await handler.list_wallets(update, make_context(["ghost"]))

    assert "Not tracking" in message.last


# ---------------------------------------------------------------- config


def test_config_rejects_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TARGET_CHAT_ID", "-100")
    monkeypatch.setenv("ADMIN_USER_IDS", "1")

    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(env_file=None)


def test_config_rejects_non_integer_chat_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TARGET_CHAT_ID", "not-a-number")
    monkeypatch.setenv("ADMIN_USER_IDS", "1")

    with pytest.raises(ConfigError, match="TARGET_CHAT_ID"):
        load_config(env_file=None)


def test_config_rejects_empty_admin_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nobody could ever modify the watchlist; better to refuse to start."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TARGET_CHAT_ID", "-100")
    monkeypatch.setenv("ADMIN_USER_IDS", "  ")

    with pytest.raises(ConfigError, match="ADMIN_USER_IDS"):
        load_config(env_file=None)


def test_config_rejects_junk_admin_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TARGET_CHAT_ID", "-100")
    monkeypatch.setenv("ADMIN_USER_IDS", "1,bob,3")

    with pytest.raises(ConfigError, match="ADMIN_USER_IDS"):
        load_config(env_file=None)


def test_config_parses_negative_chat_id_and_admin_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TARGET_CHAT_ID", "-1001234567890")  # supergroups are negative
    monkeypatch.setenv("ADMIN_USER_IDS", "1, 2 ,3")
    monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("BALANCE_CHANGE_EPSILON", raising=False)

    config = load_config(env_file=None)

    assert config.target_chat_id == -1001234567890
    assert config.admin_user_ids == frozenset({1, 2, 3})
    assert config.poll_interval_seconds == 15.0
    assert config.balance_change_epsilon == Decimal("0.01")
