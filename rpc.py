"""Async client for Aster's public JSON-RPC endpoint.

Everything here is shaped by behaviour verified against the live endpoint, which
differs from the published docs in ways that matter:

1. **Errors arrive as HTTP 200** carrying a top-level ``error`` member. Checking
   ``response.status_code`` alone reports success for a failed call.
2. **Empty collections are omitted entirely.** A wallet with no fills has no
   ``fills`` key at all rather than ``[]``; likewise ``openOrders``,
   ``perpAssets`` and ``positions``. Every accessor must tolerate absence.
3. **Privacy mode omits ``perpAssets`` and ``positions`` wholesale**, so a private
   wallet is indistinguishable on the wire from one that closed everything. The
   snapshot exposes ``privacy`` so callers can refuse to diff; see poller.py.
4. **``entryPrice``/``markPrice`` are documented but not returned in practice**, so
   both are derived when absent. See :func:`_derive_prices`.
5. **Addresses are not validated** — ``0xdeadbeef`` yields HTTP 200 and a result.
   Validation is the caller's job (handlers.py).

Money is parsed to ``Decimal`` and never float: ``walletBalance`` arrives as a
JSON *number* with more precision than a float preserves, and round-tripping it
would manufacture phantom balance-change events.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS: Final = frozenset({500, 502, 503, 504})
_MAX_ATTEMPTS: Final = 4
_BACKOFF_BASE_SECONDS: Final = 1.0
_BACKOFF_CAP_SECONDS: Final = 30.0


class AsterError(Exception):
    """Base class for every failure raised by this module."""


class AsterRpcError(AsterError):
    """The endpoint returned a JSON-RPC ``error`` member.

    Deterministic and never retried: the same request would fail identically.
    Callers should log and skip the wallet for this cycle.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message


class AsterTransportError(AsterError):
    """Network failure, timeout, or unparseable body after retries."""


class AsterRateLimited(AsterError):
    """HTTP 429 that survived the retry budget."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited by Aster (HTTP 429)")
        self.retry_after = retry_after


class AsterBanned(AsterError):
    """HTTP 418 — the IP is banned. Never retried; the poller must stand down."""


def _dec(value: Any) -> Decimal:
    """Parse an API number to Decimal, accepting both strings and JSON numbers."""
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value))
    except InvalidOperation:
        logger.warning("unparseable numeric value from Aster: %r", value)
        return Decimal(0)


@dataclass(frozen=True, slots=True)
class Balance:
    """A per-asset wallet balance from ``perpAssets``."""

    asset: str
    wallet_balance: Decimal


@dataclass(frozen=True, slots=True)
class Position:
    """One open perpetuals position, normalised and price-enriched."""

    symbol: str
    position_side: str  # BOTH | LONG | SHORT, verbatim from the API
    position_amount: Decimal  # signed: negative means short
    notional_value: Decimal
    unrealized_profit: Decimal
    entry_price: Decimal | None
    mark_price: Decimal | None
    leverage: int | None = None
    isolated: bool | None = None
    trading_product: str = "perps"

    @property
    def key(self) -> tuple[str, str]:
        """Identity used for diffing: symbol plus side."""
        return (self.symbol, self.position_side)

    @property
    def direction(self) -> str:
        """``LONG`` or ``SHORT``, resolved for one-way (``BOTH``) mode too."""
        if self.position_side in ("LONG", "SHORT"):
            return self.position_side
        return "SHORT" if self.position_amount < 0 else "LONG"

    @property
    def size(self) -> Decimal:
        """Absolute position size in tokens."""
        return abs(self.position_amount)


@dataclass(frozen=True, slots=True)
class Fill:
    """A single executed trade."""

    symbol: str
    side: str  # BUY | SELL
    price: Decimal
    qty: Decimal
    time: int  # epoch ms


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """A resting order."""

    order_id: str
    symbol: str
    side: str
    type: str
    orig_qty: Decimal
    price: Decimal


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Result of ``aster_getBalance`` for one address.

    ``privacy`` being true means balances and positions were *withheld*, not that
    they are empty — the two are identical on the wire, so callers must branch on
    this flag rather than on the emptiness of the collections.
    """

    address: str
    privacy: bool
    balances: tuple[Balance, ...]
    positions: tuple[Position, ...]


def _derive_prices(
    amount: Decimal,
    notional: Decimal,
    pnl: Decimal,
    direction: str,
) -> tuple[Decimal | None, Decimal | None]:
    """Recover ``(entry_price, mark_price)`` from the fields Aster does return.

    Aster reports ``notionalValue = markPrice * |amount|`` and
    ``unrealizedProfit = (markPrice - entryPrice) * signed_amount``, so both
    prices fall out algebraically. Checked against the documented example
    (amount 1.340, entry 84490.74932115, mark 74050.21351863, pnl -13990.31797537)
    and against a live wallet.

    Accuracy is bounded by the inputs rather than the algebra: Aster rounds
    notionalValue and unrealizedProfit to 8 decimals, so entry recovers to ~1e-13
    relative — far finer than any price precision we display, but not bit-exact.

    The signed amount is rebuilt from ``direction`` rather than trusted from the
    API: only a LONG was observable live, so whether shorts report a negative
    ``positionAmount`` or a positive one with ``positionSide: SHORT`` is
    unconfirmed. Deriving the sign here keeps both conventions correct.
    """
    size = abs(amount)
    if size == 0:
        return (None, None)

    mark = abs(notional) / size
    signed = -size if direction == "SHORT" else size
    entry = mark - (pnl / signed)
    return (entry, mark)


def _parse_position(raw: dict[str, Any], trading_product: str) -> Position | None:
    """Normalise one raw position. Returns ``None`` for flat (zero-size) entries.

    Zero-size positions are dropped so the diff treats them as absent, which is
    what "closed" means — carrying them would suppress the close notification.
    """
    amount = _dec(raw.get("positionAmount"))
    if amount == 0:
        return None

    symbol = str(raw.get("symbol") or "")
    side = str(raw.get("positionSide") or "BOTH")
    notional = _dec(raw.get("notionalValue"))
    pnl = _dec(raw.get("unrealizedProfit"))

    direction = side if side in ("LONG", "SHORT") else ("SHORT" if amount < 0 else "LONG")

    # Prefer the API's own prices when present; derive only to fill the gap.
    entry = _dec(raw["entryPrice"]) if raw.get("entryPrice") is not None else None
    mark = _dec(raw["markPrice"]) if raw.get("markPrice") is not None else None
    if entry is None or mark is None:
        derived_entry, derived_mark = _derive_prices(amount, notional, pnl, direction)
        entry = entry if entry is not None else derived_entry
        mark = mark if mark is not None else derived_mark

    leverage = raw.get("leverage")
    isolated = raw.get("isolated")

    return Position(
        symbol=symbol,
        position_side=side,
        position_amount=amount,
        notional_value=notional,
        unrealized_profit=pnl,
        entry_price=entry,
        mark_price=mark,
        leverage=int(leverage) if isinstance(leverage, (int, float)) else None,
        isolated=bool(isolated) if isinstance(isolated, bool) else None,
        trading_product=trading_product,
    )


class RateLimiter:
    """Token bucket shared by every caller, so one budget covers poller and /list.

    The lock is held across the wait, which serialises requests by design: Aster
    publishes no rate limit for this endpoint and bans abusive IPs outright
    (HTTP 418), so pacing conservatively beats discovering the ceiling.
    """

    def __init__(self, rate_per_second: float) -> None:
        self._rate = max(rate_per_second, 0.1)
        self._capacity = max(self._rate, 1.0)
        self._tokens = self._capacity
        self._updated = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = monotonic()
                elapsed = now - self._updated
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header, tolerating absence and junk."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


class AsterRpcClient:
    """Async JSON-RPC client for the Aster ``/info`` endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        rate_limiter: RateLimiter | None = None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_requests_per_second: float = 2.0,
    ) -> None:
        self._base_url = base_url
        self._limiter = rate_limiter or RateLimiter(max_requests_per_second)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsterRpcClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _call(self, method: str, params: list[Any]) -> dict[str, Any]:
        """Issue one JSON-RPC call, retrying transient failures.

        Raises:
            AsterBanned: HTTP 418. Not retried — the poller must stop entirely.
            AsterRateLimited: HTTP 429 that outlived the retry budget.
            AsterRpcError: the endpoint returned a JSON-RPC ``error`` member.
            AsterTransportError: network/timeout/parse failure after retries.
        """
        payload = {"id": {}, "jsonrpc": "2.0", "method": method, "params": params}
        last_retry_after: float | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            await self._limiter.acquire()
            try:
                response = await self._client.post(self._base_url, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise AsterTransportError(f"{method} failed: {exc}") from exc
                await self._sleep_backoff(attempt, method, f"transport error: {exc}")
                continue

            # 418 means the IP is already banned. Retrying deepens the ban.
            if response.status_code == 418:
                raise AsterBanned(
                    f"{method}: Aster returned HTTP 418 — this IP is banned."
                )

            if response.status_code == 429:
                last_retry_after = _retry_after_seconds(response)
                if attempt == _MAX_ATTEMPTS:
                    raise AsterRateLimited(last_retry_after)
                await self._sleep_backoff(
                    attempt, method, "HTTP 429", override=last_retry_after
                )
                continue

            if response.status_code in _RETRYABLE_STATUS:
                if attempt == _MAX_ATTEMPTS:
                    raise AsterTransportError(
                        f"{method}: HTTP {response.status_code} after {attempt} attempts"
                    )
                await self._sleep_backoff(
                    attempt, method, f"HTTP {response.status_code}"
                )
                continue

            if response.status_code != 200:
                raise AsterTransportError(
                    f"{method}: unexpected HTTP {response.status_code}"
                )

            try:
                body = response.json()
            except ValueError as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise AsterTransportError(f"{method}: invalid JSON body") from exc
                await self._sleep_backoff(attempt, method, "invalid JSON body")
                continue

            # Critical: errors ride on HTTP 200, so this must precede any use of
            # `result`. Deterministic — deliberately not retried.
            if isinstance(body, dict) and body.get("error"):
                err = body["error"] or {}
                raise AsterRpcError(
                    code=int(err.get("code", -1)),
                    message=str(err.get("message", "unknown error")),
                )

            result = (body or {}).get("result") if isinstance(body, dict) else None
            if not isinstance(result, dict):
                raise AsterTransportError(f"{method}: response had no result object")
            return result

        raise AsterTransportError(f"{method}: exhausted retries")

    async def _sleep_backoff(
        self,
        attempt: int,
        method: str,
        reason: str,
        *,
        override: float | None = None,
    ) -> None:
        """Exponential backoff with jitter, honouring ``Retry-After`` when given."""
        if override is not None:
            delay = override
        else:
            delay = min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_CAP_SECONDS)
            delay += random.uniform(0, delay * 0.25)  # jitter: de-sync retries
        logger.warning(
            "%s: %s (attempt %d/%d), retrying in %.2fs",
            method,
            reason,
            attempt,
            _MAX_ATTEMPTS,
            delay,
        )
        await asyncio.sleep(delay)

    async def get_balance(self, address: str) -> AccountSnapshot:
        """Fetch balances and open positions for ``address``."""
        result = await self._call("aster_getBalance", [address, "latest"])

        privacy = str(result.get("accountPrivacy", "disabled")).lower() == "enabled"

        balances = tuple(
            Balance(asset=str(item.get("asset") or ""), wallet_balance=_dec(item.get("walletBalance")))
            for item in (result.get("perpAssets") or [])
            if item.get("asset")
        )

        # positions[] groups by tradingProduct, each holding its own positions[].
        positions: list[Position] = []
        for group in result.get("positions") or []:
            product = str(group.get("tradingProduct") or "perps")
            for raw in group.get("positions") or []:
                parsed = _parse_position(raw, product)
                if parsed is not None:
                    positions.append(parsed)

        return AccountSnapshot(
            address=str(result.get("address") or address),
            privacy=privacy,
            balances=balances,
            positions=tuple(positions),
        )

    async def user_fills(
        self,
        address: str,
        symbol: str | None = None,
        from_ms: int | None = None,
        to_ms: int | None = None,
    ) -> tuple[Fill, ...]:
        """Fetch executed fills. The window must be <= 7 days or Aster errors."""
        result = await self._call(
            "aster_userFills", [address, symbol, from_ms, to_ms, "latest"]
        )
        return tuple(
            Fill(
                symbol=str(item.get("symbol") or ""),
                side=str(item.get("side") or ""),
                price=_dec(item.get("price")),
                qty=_dec(item.get("qty")),
                time=int(item.get("time") or 0),
            )
            for item in (result.get("fills") or [])
        )

    async def open_orders(
        self, address: str, symbol: str | None = None
    ) -> tuple[OpenOrder, ...]:
        """Fetch resting orders. Context for /list only; never a push trigger."""
        result = await self._call("aster_openOrders", [address, symbol, "latest"])
        return tuple(
            OpenOrder(
                order_id=str(item.get("orderId") or ""),
                symbol=str(item.get("symbol") or ""),
                side=str(item.get("side") or ""),
                type=str(item.get("type") or ""),
                orig_qty=_dec(item.get("origQty")),
                price=_dec(item.get("price")),
            )
            for item in (result.get("openOrders") or [])
        )
