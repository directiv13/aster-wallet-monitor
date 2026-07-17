"""Tests for diff logic and the polling cycle.

The diff functions are pure, so most of this needs no mocking at all. The cycle
tests use a real SQLite file so the restart cases exercise real persistence.
"""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal

import httpx

from config import FILLS_MIN_FROM_MS, Config
from db import Database, Wallet
from poller import (
    BalanceChanged,
    FillEvent,
    NowTracking,
    Poller,
    PositionClosed,
    PositionOpened,
    PositionResized,
    WentPrivate,
    diff_balances,
    diff_positions,
    fill_hash,
    fills_window,
    select_new_fills,
)
from rpc import AsterRpcClient, Fill, Position

ADDRESS = "0x1e1aabe8746cdf9166fe7c51bfc8e2438bc1714e"


def make_wallet(**overrides) -> Wallet:
    defaults = dict(
        address=ADDRESS,
        label="whale1",
        added_by=1,
        added_at=0,
        privacy_state="disabled",
        baselined=True,
    )
    defaults.update(overrides)
    return Wallet(**defaults)


def make_position(symbol="BTCUSDT", side="LONG", amount="1", pnl="0") -> Position:
    return Position(
        symbol=symbol,
        position_side=side,
        position_amount=Decimal(amount),
        notional_value=Decimal("50000"),
        unrealized_profit=Decimal(pnl),
        entry_price=Decimal("50000"),
        mark_price=Decimal("50000"),
    )


def make_fill(symbol="BTCUSDT", side="BUY", price="70000", qty="0.001", time=1_784_000_000_000) -> Fill:
    return Fill(
        symbol=symbol, side=side, price=Decimal(price), qty=Decimal(qty), time=time
    )


# --------------------------------------------------------- fill dedupe


def test_identical_fills_at_same_millisecond_are_all_reported() -> None:
    """The case a single cursor cannot handle: a sliced order.

    Five byte-identical fills at one millisecond share one hash. Counting is the
    only thing that distinguishes them.
    """
    fills = [make_fill() for _ in range(5)]
    new, observed = select_new_fills({}, fills)

    assert len(new) == 5
    assert observed[fill_hash(fills[0])] == (5, fills[0].time)


def test_previously_seen_fills_are_not_reported_again() -> None:
    fills = [make_fill() for _ in range(5)]
    stored = {fill_hash(fills[0]): 5}

    new, observed = select_new_fills(stored, fills)

    assert new == []
    assert observed[fill_hash(fills[0])] == (5, fills[0].time)


def test_only_the_surplus_of_a_repeated_fill_is_reported() -> None:
    fills = [make_fill() for _ in range(6)]
    stored = {fill_hash(fills[0]): 4}

    new, _ = select_new_fills(stored, fills)

    assert len(new) == 2


def test_stored_count_never_decreases_on_truncated_window() -> None:
    """userFills caps at 1000 records, so a window can come back truncated.

    Letting the count drop would re-notify those fills once the window moved.
    """
    fills = [make_fill() for _ in range(2)]
    stored = {fill_hash(fills[0]): 5}

    new, observed = select_new_fills(stored, fills)

    assert new == []
    assert observed[fill_hash(fills[0])][0] == 5


def test_distinct_fills_hash_differently() -> None:
    assert fill_hash(make_fill(qty="0.001")) != fill_hash(make_fill(qty="0.002"))
    assert fill_hash(make_fill(side="BUY")) != fill_hash(make_fill(side="SELL"))
    assert fill_hash(make_fill(time=1)) != fill_hash(make_fill(time=2))


def test_new_fills_are_ordered_oldest_first() -> None:
    fills = [make_fill(time=300), make_fill(time=100), make_fill(time=200)]
    new, _ = select_new_fills({}, fills)

    assert [f.time for f in new] == [100, 200, 300]


# ------------------------------------------------------- position diffing


def test_new_key_opens() -> None:
    events = diff_positions(make_wallet(), {}, {("BTCUSDT", "LONG"): make_position()})
    assert len(events) == 1
    assert isinstance(events[0], PositionOpened)


def test_missing_key_closes() -> None:
    events = diff_positions(make_wallet(), {("BTCUSDT", "LONG"): make_position()}, {})
    assert len(events) == 1
    assert isinstance(events[0], PositionClosed)


def test_size_change_resizes_with_direction() -> None:
    old = {("BTCUSDT", "LONG"): make_position(amount="1")}
    new = {("BTCUSDT", "LONG"): make_position(amount="2")}

    events = diff_positions(make_wallet(), old, new)
    assert len(events) == 1
    assert isinstance(events[0], PositionResized)
    assert events[0].increased is True

    events = diff_positions(make_wallet(), new, old)
    assert events[0].increased is False


def test_unchanged_size_emits_nothing() -> None:
    old = {("BTCUSDT", "LONG"): make_position(amount="1", pnl="0")}
    # PnL moved but size didn't: not an event.
    new = {("BTCUSDT", "LONG"): make_position(amount="1", pnl="500")}

    assert diff_positions(make_wallet(), old, new) == []


def test_decimal_equality_ignores_trailing_zeros() -> None:
    """1.10 and 1.1 are the same size; float compare would still agree here,
    but Decimal keeps that true for values floats cannot represent."""
    old = {("BTCUSDT", "LONG"): make_position(amount="1.10")}
    new = {("BTCUSDT", "LONG"): make_position(amount="1.1")}

    assert diff_positions(make_wallet(), old, new) == []


def test_long_and_short_of_one_symbol_are_separate_positions() -> None:
    old = {("BTCUSDT", "LONG"): make_position(side="LONG")}
    new = {
        ("BTCUSDT", "LONG"): make_position(side="LONG"),
        ("BTCUSDT", "SHORT"): make_position(side="SHORT"),
    }

    events = diff_positions(make_wallet(), old, new)
    assert len(events) == 1
    assert isinstance(events[0], PositionOpened)
    assert events[0].position.position_side == "SHORT"


# -------------------------------------------------------- balance diffing


def test_balance_change_above_epsilon_reports() -> None:
    events = diff_balances(
        make_wallet(), {"USDT": Decimal("100")}, {"USDT": Decimal("150")}, Decimal("0.01")
    )
    assert len(events) == 1
    assert isinstance(events[0], BalanceChanged)
    assert events[0].delta == Decimal("50")


def test_dust_below_epsilon_is_ignored() -> None:
    """Funding and fee dust would otherwise chatter every cycle."""
    events = diff_balances(
        make_wallet(),
        {"USDT": Decimal("100.000")},
        {"USDT": Decimal("100.005")},
        Decimal("0.01"),
    )
    assert events == []


def test_change_exactly_at_epsilon_is_ignored() -> None:
    events = diff_balances(
        make_wallet(), {"USDT": Decimal("100")}, {"USDT": Decimal("100.01")}, Decimal("0.01")
    )
    assert events == []


def test_new_asset_reports_from_zero() -> None:
    events = diff_balances(make_wallet(), {}, {"USDT": Decimal("500")}, Decimal("0.01"))
    assert len(events) == 1
    assert events[0].old == Decimal("0")
    assert events[0].delta == Decimal("500")


def test_high_precision_balance_delta_is_exact() -> None:
    """The precision that a float round-trip would destroy."""
    events = diff_balances(
        make_wallet(),
        {"USDT": Decimal("996222.26328264")},
        {"USDT": Decimal("996222.26328264")},
        Decimal("0.01"),
    )
    assert events == []


# ---------------------------------------------------------- fills window


def test_window_never_starts_before_the_documented_floor() -> None:
    from_ms, _ = fills_window(None, FILLS_MIN_FROM_MS + 1000, 120_000)
    assert from_ms >= FILLS_MIN_FROM_MS


def test_window_stays_inside_seven_days() -> None:
    now = 1_784_132_166_980
    from_ms, to_ms = fills_window(None, now, 120_000)
    assert to_ms - from_ms < 7 * 24 * 60 * 60 * 1000


def test_window_overlaps_backwards_from_cursor() -> None:
    now = 1_784_132_166_980
    cursor = now - 10_000
    from_ms, to_ms = fills_window(cursor, now, 120_000)
    assert from_ms == cursor - 120_000
    assert to_ms == now


def test_window_from_never_exceeds_to() -> None:
    now = 1_784_132_166_980
    from_ms, to_ms = fills_window(now + 999_999, now, 0)
    assert from_ms <= to_ms


# ------------------------------------------------------------ full cycle


def make_config(tmp_path, **overrides) -> Config:
    defaults = dict(
        telegram_bot_token="token",
        target_chat_id=-100,
        admin_user_ids=frozenset({1}),
        poll_interval_seconds=15.0,
        balance_change_epsilon=Decimal("0.01"),
        rpc_base_url="https://example.invalid/info",
        db_path=tmp_path / "test.db",
        heartbeat_path=tmp_path / "heartbeat",
        max_requests_per_second=1000.0,
        fill_overlap_seconds=120,
        request_timeout_seconds=5.0,
        ban_cooldown_seconds=300.0,
    )
    defaults.update(overrides)
    return Config(**defaults)


def balance_result(positions=(), balances=(("USDT", 1000),), privacy="disabled") -> dict:
    return {
        "address": ADDRESS,
        "accountPrivacy": privacy,
        "perpAssets": [{"asset": a, "walletBalance": b} for a, b in balances],
        "positions": [{"tradingProduct": "perps", "positions": list(positions)}],
    }


def raw_position(symbol="BTCUSDT", amount="1", side="LONG") -> dict:
    return {
        "symbol": symbol,
        "positionAmount": amount,
        "positionSide": side,
        "notionalValue": "50000",
        "unrealizedProfit": "100",
    }


class Harness:
    """Wires a Poller to a scripted transport and a real database."""

    def __init__(self, tmp_path, responses) -> None:
        self.config = make_config(tmp_path)
        self.responses = responses  # method -> list of result dicts (consumed)
        self.dispatched: list = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        import json

        method = json.loads(request.content)["method"]
        queue = self.responses.get(method)
        result = queue.pop(0) if isinstance(queue, list) and queue else (queue or {})
        if isinstance(result, list):
            result = {}
        return httpx.Response(200, json={"result": result, "id": {}, "jsonrpc": "2.0"})

    async def __aenter__(self):
        self.db = Database(self.config.db_path)
        await self.db.connect()
        self.rpc = AsterRpcClient(
            self.config.rpc_base_url,
            transport=httpx.MockTransport(self._handler),
            max_requests_per_second=1000.0,
        )
        self.poller = Poller(self.config, self.db, self.rpc, self._dispatch)
        return self

    async def _dispatch(self, events) -> None:
        self.dispatched.extend(events)

    async def __aexit__(self, *_exc) -> None:
        await self.rpc.aclose()
        await self.db.close()


async def test_baseline_does_not_flood_opened_events(tmp_path) -> None:
    """Acceptance #5: adding a whale with open positions must stay quiet."""
    responses = {
        "aster_getBalance": balance_result(
            positions=[raw_position("BTCUSDT"), raw_position("ETHUSDT"), raw_position("SOLUSDT")]
        ),
        "aster_userFills": {"fills": [{"symbol": "BTCUSDT", "side": "BUY", "price": "1", "qty": "1", "time": 1_784_000_000_000}]},
    }
    async with Harness(tmp_path, responses) as h:
        await h.db.add_wallet(ADDRESS, "whale1", 1)
        events = await h.poller.run_cycle()

    assert len(events) == 1
    assert isinstance(events[0], NowTracking)
    assert len(events[0].snapshot.positions) == 3


async def test_no_reevents_on_second_cycle(tmp_path) -> None:
    responses = {
        "aster_getBalance": balance_result(positions=[raw_position()]),
        "aster_userFills": {"fills": [{"symbol": "BTCUSDT", "side": "BUY", "price": "1", "qty": "1", "time": 1_784_000_000_000}]},
    }
    async with Harness(tmp_path, responses) as h:
        await h.db.add_wallet(ADDRESS, "whale1", 1)
        await h.poller.run_cycle()  # baseline
        events = await h.poller.run_cycle()

    assert events == []


async def test_state_survives_restart_without_renotifying(tmp_path) -> None:
    """Acceptance #3/#5: reopening the database must not replay old events."""
    responses = {
        "aster_getBalance": balance_result(positions=[raw_position()]),
        "aster_userFills": {"fills": [{"symbol": "BTCUSDT", "side": "BUY", "price": "1", "qty": "1", "time": 1_784_000_000_000}]},
    }

    async with Harness(tmp_path, responses) as h:
        await h.db.add_wallet(ADDRESS, "whale1", 1)
        await h.poller.run_cycle()

    # Fresh process, same database file, identical API state.
    async with Harness(tmp_path, responses) as h2:
        events = await h2.poller.run_cycle()

    assert events == []


async def test_open_close_and_resize_each_emit_once(tmp_path) -> None:
    """Acceptance #3, the happy path."""
    responses = {
        "aster_getBalance": [
            balance_result(),                                   # baseline: flat
            balance_result(positions=[raw_position(amount="1")]),  # opened
            balance_result(positions=[raw_position(amount="3")]),  # increased
            balance_result(),                                   # closed
        ],
        "aster_userFills": {},
    }
    async with Harness(tmp_path, responses) as h:
        await h.db.add_wallet(ADDRESS, "whale1", 1)
        await h.poller.run_cycle()
        opened = await h.poller.run_cycle()
        resized = await h.poller.run_cycle()
        closed = await h.poller.run_cycle()

    assert len(opened) == 1 and isinstance(opened[0], PositionOpened)
    assert len(resized) == 1 and isinstance(resized[0], PositionResized)
    assert resized[0].increased is True
    assert len(closed) == 1 and isinstance(closed[0], PositionClosed)


async def test_new_fill_emits_once(tmp_path) -> None:
    fill = {"symbol": "BTCUSDT", "side": "BUY", "price": "70000", "qty": "0.5", "time": 1_784_000_000_000}
    later = {"symbol": "BTCUSDT", "side": "SELL", "price": "71000", "qty": "0.5", "time": 1_784_000_060_000}
    responses = {
        "aster_getBalance": balance_result(),
        "aster_userFills": [{"fills": [fill]}, {"fills": [fill, later]}, {"fills": [fill, later]}],
    }
    async with Harness(tmp_path, responses) as h:
        await h.db.add_wallet(ADDRESS, "whale1", 1)
        await h.poller.run_cycle()  # baseline marks `fill` as seen
        second = await h.poller.run_cycle()
        third = await h.poller.run_cycle()

    assert len(second) == 1
    assert isinstance(second[0], FillEvent)
    assert second[0].fill.side == "SELL"
    assert third == []


async def test_balance_change_emits(tmp_path) -> None:
    responses = {
        "aster_getBalance": [
            balance_result(balances=(("USDT", 1000),)),
            balance_result(balances=(("USDT", 1500),)),
        ],
        "aster_userFills": {},
    }
    async with Harness(tmp_path, responses) as h:
        await h.db.add_wallet(ADDRESS, "whale1", 1)
        await h.poller.run_cycle()
        events = await h.poller.run_cycle()

    assert len(events) == 1
    assert isinstance(events[0], BalanceChanged)
    assert events[0].delta == Decimal("500")


async def test_going_private_notifies_once_and_never_fires_false_closes(tmp_path) -> None:
    """The bug this guard exists for.

    Privacy withholds positions and balances rather than reporting them empty. A
    naive diff would read that as "closed everything, balance drained".
    """
    responses = {
        "aster_getBalance": [
            balance_result(positions=[raw_position("BTCUSDT"), raw_position("ETHUSDT")]),
            {"address": ADDRESS, "accountPrivacy": "enabled"},
            {"address": ADDRESS, "accountPrivacy": "enabled"},
        ],
        "aster_userFills": {},
    }
    async with Harness(tmp_path, responses) as h:
        await h.db.add_wallet(ADDRESS, "whale1", 1)
        await h.poller.run_cycle()  # baseline with 2 positions
        went_private = await h.poller.run_cycle()
        still_private = await h.poller.run_cycle()

        # Snapshot must survive so privacy can be switched back off cleanly.
        stored = await h.db.get_positions(ADDRESS)

    assert len(went_private) == 1
    assert isinstance(went_private[0], WentPrivate)
    assert still_private == []  # notified once, not every cycle
    assert len(stored) == 2
    assert not any(isinstance(e, (PositionClosed, BalanceChanged)) for e in went_private)


async def test_rpc_failure_leaves_state_intact_and_loop_alive(tmp_path) -> None:
    """A failed fetch is not an empty wallet."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        method = json.loads(request.content)["method"]
        if method == "aster_getBalance":
            calls["n"] += 1
            if calls["n"] > 1:
                return httpx.Response(
                    200, json={"error": {"code": -32603, "message": "Internal error"}}
                )
            return httpx.Response(200, json={"result": balance_result(positions=[raw_position()])})
        return httpx.Response(200, json={"result": {}})

    config = make_config(tmp_path)
    db = Database(config.db_path)
    await db.connect()
    rpc_client = AsterRpcClient(
        config.rpc_base_url,
        transport=httpx.MockTransport(handler),
        max_requests_per_second=1000.0,
    )
    dispatched: list = []

    async def dispatch(events):
        dispatched.extend(events)

    poller = Poller(config, db, rpc_client, dispatch)
    await db.add_wallet(ADDRESS, "whale1", 1)
    await poller.run_cycle()  # baseline

    events = await poller.run_cycle()  # errors out
    stored = await db.get_positions(ADDRESS)

    await rpc_client.aclose()
    await db.close()

    assert events == []  # no false closes
    assert len(stored) == 1  # snapshot preserved


async def test_ban_pauses_the_whole_poller(tmp_path) -> None:
    config = make_config(tmp_path, ban_cooldown_seconds=600.0)
    db = Database(config.db_path)
    await db.connect()

    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(418)

    rpc_client = AsterRpcClient(
        config.rpc_base_url,
        transport=httpx.MockTransport(handler),
        max_requests_per_second=1000.0,
    )

    async def dispatch(events):
        pass

    poller = Poller(config, db, rpc_client, dispatch)
    await db.add_wallet(ADDRESS, "whale1", 1)
    await db.add_wallet("0x" + "b" * 40, "whale2", 1)

    await poller.run_cycle()
    after_ban = calls["n"]
    await poller.run_cycle()  # must not touch the network at all

    await rpc_client.aclose()
    await db.close()

    assert after_ban == 1  # stopped at the first wallet, didn't try the second
    assert calls["n"] == after_ban


async def test_run_cycle_writes_a_fresh_heartbeat(tmp_path) -> None:
    """The HEALTHCHECK's liveness signal: every cycle refreshes the file."""
    from datetime import datetime

    responses = {
        "aster_getBalance": balance_result(),
        "aster_userFills": {},
    }
    async with Harness(tmp_path, responses) as h:
        await h.poller.run_cycle()  # empty watchlist still heartbeats
        first = h.config.heartbeat_path.read_text()

        # Parses as a recent UTC timestamp.
        stamp = datetime.fromisoformat(first)
        assert (datetime.now(UTC) - stamp).total_seconds() < 5

        await h.poller.run_cycle()
        second = h.config.heartbeat_path.read_text()

    assert h.config.heartbeat_path.exists()
    assert second >= first  # rewritten, never removed


def test_effective_interval_widens_for_large_watchlists(tmp_path) -> None:
    config = make_config(tmp_path, poll_interval_seconds=15.0, max_requests_per_second=2.0)
    poller = Poller(config, None, None, None)  # type: ignore[arg-type]

    assert poller.effective_interval(10) == 15.0  # 20 calls / 2rps = 10s, fits
    assert poller.effective_interval(50) == 50.0  # 100 calls / 2rps = 50s, widens
