"""Tests for the Aster RPC client.

Rate limiting is exercised against a mock transport and never against the live
endpoint: probing for the real ceiling is exactly the behaviour that earns an
HTTP 418 IP ban.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

import rpc
from rpc import (
    AsterBanned,
    AsterRateLimited,
    AsterRpcClient,
    AsterRpcError,
    AsterTransportError,
)

# Captured verbatim from the live endpoint for
# 0x1e1aabe8746cdf9166fe7c51bfc8e2438bc1714e. Note what is absent: no
# entryPrice, no markPrice, no leverage, no isolated, no marginValue.
LIVE_WHALE_RESULT = {
    "address": "0x1e1aabe8746cdf9166fe7c51bfc8e2438bc1714e",
    "accountPrivacy": "disabled",
    "perpAssets": [{"asset": "USDT", "walletBalance": 996222.26328264}],
    "positions": [
        {
            "tradingProduct": "perps",
            "positions": [
                {
                    "id": "98000030018085_AKEUSDT_LONG",
                    "symbol": "AKEUSDT",
                    "positionAmount": "1525961628",
                    "positionSide": "LONG",
                    "notionalValue": "1079297.39986812",
                    "unrealizedProfit": "-33007.0198572252612",
                    "cumRealized": "0.00000000",
                }
            ],
        }
    ],
}

# The documented example, which does carry entryPrice/markPrice.
DOCS_RESULT = {
    "address": "0x690931c",
    "accountPrivacy": "disabled",
    "perpAssets": [{"asset": "USDT", "walletBalance": 9049204461.84438644}],
    "positions": [
        {
            "tradingProduct": "perps",
            "positions": [
                {
                    "id": "98000000000389_BTCUSDT_BOTH",
                    "symbol": "BTCUSDT",
                    "positionAmount": "1.340",
                    "entryPrice": "84490.74932115",
                    "unrealizedProfit": "-13990.31797537",
                    "notionalValue": "99227.28611496",
                    "markPrice": "74050.21351863",
                    "leverage": 1,
                    "isolated": False,
                    "positionSide": "BOTH",
                    "marginValue": "99227.28611496",
                }
            ],
        }
    ],
}


def make_client(handler, **kwargs) -> AsterRpcClient:
    return AsterRpcClient(
        "https://example.invalid/info",
        transport=httpx.MockTransport(handler),
        max_requests_per_second=1000.0,  # keep the limiter out of the way
        **kwargs,
    )


def json_ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"result": result, "id": {}, "jsonrpc": "2.0"})


async def test_derives_entry_and_mark_when_api_omits_them() -> None:
    """The live endpoint omits both prices; they must be recovered exactly."""
    async with make_client(lambda _req: json_ok(LIVE_WHALE_RESULT)) as client:
        snapshot = await client.get_balance("0x1e1a")

    position = snapshot.positions[0]
    assert position.symbol == "AKEUSDT"
    assert position.direction == "LONG"

    # mark = notional / size
    assert position.mark_price == Decimal("1079297.39986812") / Decimal("1525961628")
    # entry recovers such that pnl = (mark - entry) * size
    recomputed = (position.mark_price - position.entry_price) * position.position_amount
    assert abs(recomputed - position.unrealized_profit) < Decimal("1e-9")


async def test_prefers_api_prices_over_derivation() -> None:
    """When Aster does send prices, they are used verbatim, not recomputed."""
    async with make_client(lambda _req: json_ok(DOCS_RESULT)) as client:
        snapshot = await client.get_balance("0x690931c")

    position = snapshot.positions[0]
    assert position.entry_price == Decimal("84490.74932115")
    assert position.mark_price == Decimal("74050.21351863")
    assert position.leverage == 1
    assert position.isolated is False


async def test_derivation_matches_documented_example() -> None:
    """Strip the documented prices and confirm derivation reproduces them.

    Agreement is bounded by the inputs, not the algebra: Aster rounds
    notionalValue and unrealizedProfit to 8 decimals, so entry recovers to about
    1e-13 relative (84490.74932114 vs the documented 84490.74932115) and cannot
    do better. Orders of magnitude finer than any price we display.
    """
    stripped = {
        **DOCS_RESULT,
        "positions": [
            {
                "tradingProduct": "perps",
                "positions": [
                    {
                        k: v
                        for k, v in DOCS_RESULT["positions"][0]["positions"][0].items()
                        if k not in ("entryPrice", "markPrice")
                    }
                ],
            }
        ],
    }
    async with make_client(lambda _req: json_ok(stripped)) as client:
        snapshot = await client.get_balance("0x690931c")

    position = snapshot.positions[0]
    assert abs(position.entry_price - Decimal("84490.74932115")) < Decimal("1e-6")
    assert abs(position.mark_price - Decimal("74050.21351863")) < Decimal("1e-6")


async def test_balance_precision_survives_as_decimal() -> None:
    """walletBalance arrives as a JSON number; float would corrupt it."""
    async with make_client(lambda _req: json_ok(LIVE_WHALE_RESULT)) as client:
        snapshot = await client.get_balance("0x1e1a")

    assert snapshot.balances[0].wallet_balance == Decimal("996222.26328264")


async def test_absent_collections_do_not_raise() -> None:
    """Empty results omit the keys entirely rather than sending []."""
    async with make_client(
        lambda _req: json_ok({"address": "0x1", "accountPrivacy": "disabled"})
    ) as client:
        snapshot = await client.get_balance("0x1")
        fills = await client.user_fills("0x1")
        orders = await client.open_orders("0x1")

    assert snapshot.balances == ()
    assert snapshot.positions == ()
    assert fills == ()
    assert orders == ()


async def test_privacy_enabled_is_flagged() -> None:
    async with make_client(
        lambda _req: json_ok({"address": "0x1", "accountPrivacy": "enabled"})
    ) as client:
        snapshot = await client.get_balance("0x1")

    assert snapshot.privacy is True
    assert snapshot.positions == ()  # withheld, NOT empty — see poller


async def test_zero_size_positions_are_dropped() -> None:
    result = {
        "address": "0x1",
        "accountPrivacy": "disabled",
        "positions": [
            {
                "tradingProduct": "perps",
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmount": "0",
                        "positionSide": "BOTH",
                        "notionalValue": "0",
                        "unrealizedProfit": "0",
                    }
                ],
            }
        ],
    }
    async with make_client(lambda _req: json_ok(result)) as client:
        snapshot = await client.get_balance("0x1")

    assert snapshot.positions == ()


async def test_short_direction_from_negative_amount_in_both_mode() -> None:
    """One-way mode reports SHORT as a negative amount under positionSide BOTH."""
    result = {
        "address": "0x1",
        "accountPrivacy": "disabled",
        "positions": [
            {
                "tradingProduct": "perps",
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmount": "-2",
                        "positionSide": "BOTH",
                        "notionalValue": "100000",
                        "unrealizedProfit": "1000",
                    }
                ],
            }
        ],
    }
    async with make_client(lambda _req: json_ok(result)) as client:
        position = (await client.get_balance("0x1")).positions[0]

    assert position.direction == "SHORT"
    assert position.mark_price == Decimal("50000")
    # A profitable short entered above mark: pnl = (entry - mark) * size
    assert position.entry_price == Decimal("50500")


async def test_short_direction_from_position_side() -> None:
    """Hedge mode reports SHORT via positionSide with a positive amount."""
    result = {
        "address": "0x1",
        "accountPrivacy": "disabled",
        "positions": [
            {
                "tradingProduct": "perps",
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmount": "2",
                        "positionSide": "SHORT",
                        "notionalValue": "100000",
                        "unrealizedProfit": "1000",
                    }
                ],
            }
        ],
    }
    async with make_client(lambda _req: json_ok(result)) as client:
        position = (await client.get_balance("0x1")).positions[0]

    assert position.direction == "SHORT"
    assert position.entry_price == Decimal("50500")


async def test_jsonrpc_error_on_http_200_raises() -> None:
    """Aster returns errors with a 200 status; status alone means nothing."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": {},
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": "Internal error: Time range exceeds 7 days"},
            },
        )

    async with make_client(handler) as client:
        with pytest.raises(AsterRpcError) as excinfo:
            await client.user_fills("0x1")

    assert excinfo.value.code == -32603
    assert "7 days" in excinfo.value.message


async def test_jsonrpc_error_is_not_retried() -> None:
    """A deterministic error must not burn the retry budget."""
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"error": {"code": -32601, "message": "Method not found"}}
        )

    async with make_client(handler) as client:
        with pytest.raises(AsterRpcError):
            await client.get_balance("0x1")

    assert calls == 1


async def test_429_backs_off_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance #4: a 429 triggers backoff and the call resumes, no crash."""
    monkeypatch.setattr(rpc, "_BACKOFF_BASE_SECONDS", 0.001)
    attempts = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return json_ok(LIVE_WHALE_RESULT)

    async with make_client(handler) as client:
        snapshot = await client.get_balance("0x1e1a")

    assert attempts == 3
    assert snapshot.positions[0].symbol == "AKEUSDT"


async def test_429_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(rpc.asyncio, "sleep", fake_sleep)
    attempts = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return json_ok({"address": "0x1", "accountPrivacy": "disabled"})

    async with make_client(handler) as client:
        await client.get_balance("0x1")

    assert 7.0 in slept


async def test_persistent_429_raises_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc, "_BACKOFF_BASE_SECONDS", 0.001)

    async with make_client(lambda _req: httpx.Response(429)) as client:
        with pytest.raises(AsterRateLimited):
            await client.get_balance("0x1")


async def test_418_raises_immediately_without_retry() -> None:
    """Retrying a ban deepens it, so 418 must never be retried."""
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(418)

    async with make_client(handler) as client:
        with pytest.raises(AsterBanned):
            await client.get_balance("0x1")

    assert calls == 1


async def test_5xx_retries_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc, "_BACKOFF_BASE_SECONDS", 0.001)
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async with make_client(handler) as client:
        with pytest.raises(AsterTransportError):
            await client.get_balance("0x1")

    assert calls == rpc._MAX_ATTEMPTS


async def test_timeout_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc, "_BACKOFF_BASE_SECONDS", 0.001)

    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    async with make_client(handler) as client:
        with pytest.raises(AsterTransportError):
            await client.get_balance("0x1")


async def test_request_envelope_shape() -> None:
    """params order and the odd `"id": {}` envelope are what Aster expects."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return json_ok({"address": "0x1", "accountPrivacy": "disabled"})

    async with make_client(handler) as client:
        await client.user_fills("0xabc", symbol="BTCUSDT", from_ms=1, to_ms=2)

    assert seen["jsonrpc"] == "2.0"
    assert seen["id"] == {}
    assert seen["method"] == "aster_userFills"
    assert seen["params"] == ["0xabc", "BTCUSDT", 1, 2, "latest"]
