"""Polling cycle, state diffing, and notification dispatch.

The diff functions at the top are pure: they take old and new state and return
events, touching neither the network nor the database. That keeps the rules that
decide whether a user gets pinged testable without mocking anything.

Three invariants hold the whole design together:

* **A failed fetch is not an empty wallet.** Any RPC failure skips the wallet with
  its stored state intact. Committing an "empty" snapshot on error would fire a
  close notification for every open position and re-fire an open on recovery.
* **A private wallet is not an empty wallet.** Privacy mode withholds positions
  and balances rather than reporting them empty, so diffing is skipped entirely
  while privacy is on.
* **A newly added wallet is baselined silently**, so adding a whale with thirty
  open positions does not fire thirty "opened" messages.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from config import FILLS_MAX_RANGE_MS, FILLS_MIN_FROM_MS, Config
from db import Database, Wallet
from rpc import (
    AccountSnapshot,
    AsterBanned,
    AsterError,
    AsterRpcClient,
    Fill,
    Position,
)

logger = logging.getLogger(__name__)

# Keeps the requested window strictly inside Aster's 7-day limit despite clock
# skew and in-flight latency; a window even a millisecond over is a hard error.
_WINDOW_MARGIN_MS = 60_000


# ----------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for anything worth notifying about."""

    wallet: Wallet


@dataclass(frozen=True, slots=True)
class FillEvent(Event):
    fill: Fill


@dataclass(frozen=True, slots=True)
class PositionOpened(Event):
    position: Position


@dataclass(frozen=True, slots=True)
class PositionClosed(Event):
    position: Position


@dataclass(frozen=True, slots=True)
class PositionResized(Event):
    old: Position
    new: Position

    @property
    def increased(self) -> bool:
        return abs(self.new.position_amount) > abs(self.old.position_amount)


@dataclass(frozen=True, slots=True)
class BalanceChanged(Event):
    asset: str
    old: Decimal
    new: Decimal

    @property
    def delta(self) -> Decimal:
        return self.new - self.old


@dataclass(frozen=True, slots=True)
class WentPrivate(Event):
    """Privacy flipped on; further activity is hidden from us."""


@dataclass(frozen=True, slots=True)
class NowTracking(Event):
    """One-time summary emitted when a wallet is first baselined."""

    snapshot: AccountSnapshot


Dispatch = Callable[[Sequence[Event]], Awaitable[None]]


# ------------------------------------------------------------ pure diffing


def fill_hash(fill: Fill) -> str:
    """Stable identity for a fill.

    Aster exposes no trade id — its docs state dedupe must "rely on symbol, time,
    price, and qty combination" — so identical fills at the same millisecond
    collapse to one hash and are told apart by counting occurrences instead.
    """
    raw = f"{fill.symbol}|{fill.side}|{fill.price}|{fill.qty}|{fill.time}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def select_new_fills(
    stored_counts: Mapping[str, int], fetched: Sequence[Fill]
) -> tuple[list[Fill], dict[str, tuple[int, int]]]:
    """Pick out fills not yet notified, by occurrence count per hash.

    A 10 BTC order sliced into five equal 2 BTC fills at one millisecond produces
    five byte-identical records. Counting is what distinguishes "five fills I've
    already reported" from "a sixth just landed".

    Returns:
        The new fills (oldest first) and ``{hash: (count, fill_time)}`` to persist.

    The persisted count never decreases: Aster caps ``userFills`` at 1000 records,
    so a busy wallet can return a truncated view of a window it previously
    returned in full. Letting the count drop would re-notify those fills once the
    window moved on.
    """
    grouped: dict[str, list[Fill]] = defaultdict(list)
    for fill in fetched:
        grouped[fill_hash(fill)].append(fill)

    new_fills: list[Fill] = []
    observed: dict[str, tuple[int, int]] = {}

    for digest, fills in grouped.items():
        already_seen = stored_counts.get(digest, 0)
        observed[digest] = (max(len(fills), already_seen), fills[0].time)
        if len(fills) > already_seen:
            new_fills.extend(fills[already_seen:])

    new_fills.sort(key=lambda f: f.time)
    return new_fills, observed


def diff_positions(
    wallet: Wallet,
    old: Mapping[tuple[str, str], Position],
    new: Mapping[tuple[str, str], Position],
) -> list[Event]:
    """Diff positions keyed by ``(symbol, position_side)``.

    Sizes compare as ``Decimal``, so ``1.10`` and ``1.1`` are the same size and
    raise no event — a float comparison here would emit spurious resizes.
    """
    events: list[Event] = []

    for key in sorted(new):
        position = new[key]
        previous = old.get(key)
        if previous is None:
            events.append(PositionOpened(wallet=wallet, position=position))
        elif previous.position_amount != position.position_amount:
            events.append(PositionResized(wallet=wallet, old=previous, new=position))

    for key in sorted(old):
        if key not in new:
            events.append(PositionClosed(wallet=wallet, position=old[key]))

    return events


def diff_balances(
    wallet: Wallet,
    old: Mapping[str, Decimal],
    new: Mapping[str, Decimal],
    epsilon: Decimal,
) -> list[Event]:
    """Diff per-asset balances, ignoring movements within ``epsilon``.

    The epsilon suppresses funding and fee dust, which would otherwise make a
    merely-open position chatter every cycle.
    """
    events: list[Event] = []
    for asset in sorted(set(old) | set(new)):
        before = old.get(asset, Decimal(0))
        after = new.get(asset, Decimal(0))
        if abs(after - before) > epsilon:
            events.append(
                BalanceChanged(wallet=wallet, asset=asset, old=before, new=after)
            )
    return events


def fills_window(
    last_fill_time: int | None, now_ms: int, overlap_ms: int
) -> tuple[int, int]:
    """Build a ``userFills`` window that Aster will accept.

    Clamped to the API's constraints: at most 7 days wide, never starting before
    the documented floor. The overlap re-reads recent fills so a fill landing
    mid-cycle isn't skipped; the occurrence counter absorbs the duplicates.
    """
    to_ms = now_ms
    earliest_allowed = max(FILLS_MIN_FROM_MS, now_ms - FILLS_MAX_RANGE_MS + _WINDOW_MARGIN_MS)

    if last_fill_time is None:
        from_ms = earliest_allowed
    else:
        from_ms = max(last_fill_time - overlap_ms, earliest_allowed)

    return (min(from_ms, to_ms), to_ms)


# ---------------------------------------------------------------- poller


class Poller:
    """Drives the polling cycle and hands events to a dispatcher."""

    def __init__(
        self,
        config: Config,
        db: Database,
        rpc: AsterRpcClient,
        dispatch: Dispatch,
    ) -> None:
        self._config = config
        self._db = db
        self._rpc = rpc
        self._dispatch = dispatch
        self._banned_until: float = 0.0

    def effective_interval(self, wallet_count: int) -> float:
        """Widen the interval when the watchlist outgrows the rate budget.

        Each wallet costs two calls per cycle. Aster publishes no limit for this
        endpoint and bans abusive IPs outright, so the configured interval is a
        floor on speed, not a promise.
        """
        configured = self._config.poll_interval_seconds
        if wallet_count <= 0:
            return configured
        needed = (wallet_count * 2) / self._config.max_requests_per_second
        if needed > configured:
            logger.warning(
                "%d wallets need ~%.1fs per cycle at %.1f req/s; widening interval "
                "from %.1fs to avoid a rate-limit ban",
                wallet_count,
                needed,
                self._config.max_requests_per_second,
                configured,
            )
            return needed
        return configured

    async def run_cycle(self) -> list[Event]:
        """Poll every tracked wallet once. Never raises for a single failure."""
        try:
            if time.monotonic() < self._banned_until:
                remaining = self._banned_until - time.monotonic()
                logger.warning("poller paused for %.0fs after HTTP 418 ban", remaining)
                return []

            wallets = await self._db.list_wallets()
            if not wallets:
                return []

            all_events: list[Event] = []
            for wallet in wallets:
                try:
                    events = await self.poll_wallet(wallet)
                except AsterBanned as exc:
                    # Whole-poller stand-down: continuing would deepen the ban.
                    self._banned_until = time.monotonic() + self._config.ban_cooldown_seconds
                    logger.error(
                        "%s — pausing poller for %.0fs",
                        exc,
                        self._config.ban_cooldown_seconds,
                    )
                    break
                except Exception:  # one bad wallet must not kill the loop
                    logger.exception("unhandled error polling %s", wallet.address)
                    continue

                if events:
                    all_events.extend(events)
                    await self._dispatch(events)

            await self._prune()
            return all_events
        finally:
            # Heartbeat every cycle, on every exit path — including an empty
            # watchlist and a ban stand-down. It signals "the loop is turning",
            # not "polling succeeded"; restarting a healthy-but-paused process on
            # a ban would only churn.
            self._write_heartbeat()

    async def poll_wallet(self, wallet: Wallet) -> list[Event]:
        """Poll one wallet and return the events its changes warrant.

        Raises:
            AsterBanned: propagated so the caller can halt the whole poller.
        """
        address = wallet.address

        try:
            snapshot = await self._rpc.get_balance(address)
        except AsterBanned:
            raise
        except AsterError as exc:
            # State is deliberately left untouched: an error is not an empty wallet.
            logger.warning("skipping %s this cycle: %s", address, exc)
            return []

        if snapshot.privacy:
            return await self._handle_private(wallet)

        # Fills are best-effort. If they fail we still diff positions and
        # balances, and leave the fill ledger untouched so nothing is lost.
        fetched_fills: tuple[Fill, ...] | None = None
        try:
            cursor = await self._db.get_fill_cursor(address)
            from_ms, to_ms = fills_window(
                cursor, _now_ms(), self._config.fill_overlap_seconds * 1000
            )
            fetched_fills = await self._rpc.user_fills(
                address, from_ms=from_ms, to_ms=to_ms
            )
        except AsterBanned:
            raise
        except AsterError as exc:
            logger.warning("fills unavailable for %s this cycle: %s", address, exc)

        stored_counts = await self._db.get_seen_fill_counts(address)
        new_fills: list[Fill] = []
        observed: dict[str, tuple[int, int]] = {}
        if fetched_fills is not None:
            new_fills, observed = select_new_fills(stored_counts, fetched_fills)

        last_fill_time = max((f.time for f in fetched_fills), default=None) if fetched_fills else None

        if not wallet.baselined:
            return await self._baseline(wallet, snapshot, observed, last_fill_time)

        old_positions = await self._db.get_positions(address)
        old_balances = await self._db.get_balances(address)
        new_positions = {p.key: p for p in snapshot.positions}
        new_balances = {b.asset: b.wallet_balance for b in snapshot.balances}

        events: list[Event] = [FillEvent(wallet=wallet, fill=f) for f in new_fills]
        events.extend(diff_positions(wallet, old_positions, new_positions))
        events.extend(
            diff_balances(
                wallet, old_balances, new_balances, self._config.balance_change_epsilon
            )
        )

        await self._db.commit_cycle(
            address,
            balances=snapshot.balances,
            positions=snapshot.positions,
            fill_counts=observed,
            last_fill_time=last_fill_time,
            privacy_state="disabled",
        )
        return events

    async def _handle_private(self, wallet: Wallet) -> list[Event]:
        """Privacy is on: notify once on the transition, and diff nothing.

        Stored snapshots are left alone. Overwriting them with the withheld
        (empty) view would fire a close for every position and, when privacy is
        switched back off, fire them all open again.
        """
        events: list[Event] = []
        if wallet.privacy_state == "disabled":
            events.append(WentPrivate(wallet=wallet))
        await self._db.set_privacy_state(wallet.address, "enabled")
        return events

    async def _baseline(
        self,
        wallet: Wallet,
        snapshot: AccountSnapshot,
        observed: Mapping[str, tuple[int, int]],
        last_fill_time: int | None,
    ) -> list[Event]:
        """Record a newly added wallet's state without firing per-change events.

        Recent fills are marked seen here, not notified: they predate tracking.
        """
        await self._db.commit_cycle(
            wallet.address,
            balances=snapshot.balances,
            positions=snapshot.positions,
            fill_counts=dict(observed),
            last_fill_time=last_fill_time,
            privacy_state="disabled",
            baselined=True,
        )
        logger.info(
            "baselined %s with %d position(s)", wallet.address, len(snapshot.positions)
        )
        return [NowTracking(wallet=wallet, snapshot=snapshot)]

    async def _prune(self) -> None:
        """Drop dedupe rows that fell out of the API's 7-day fills window."""
        try:
            cutoff = _now_ms() - FILLS_MAX_RANGE_MS
            removed = await self._db.prune_seen_fills(cutoff)
            if removed:
                logger.debug("pruned %d stale seen_fills rows", removed)
        except Exception:
            logger.exception("failed to prune seen_fills")

    def _write_heartbeat(self) -> None:
        """Refresh the liveness file the container HEALTHCHECK reads.

        Written atomically (temp sibling + ``os.replace``) so a reader never sees
        a half-written timestamp. Any I/O failure is logged and swallowed: a
        heartbeat that cannot be written must never take down the poll loop.
        """
        path = self._config.heartbeat_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).isoformat()
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(stamp, encoding="ascii")
            os.replace(tmp, path)
        except OSError:
            logger.exception("failed to write heartbeat to %s", path)


def _now_ms() -> int:
    return int(time.time() * 1000)
