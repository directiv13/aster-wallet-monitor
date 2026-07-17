"""SQLite persistence for the watchlist and last-seen wallet state.

Two conventions worth keeping:

* **Every numeric column is TEXT, parsed to ``Decimal``.** SQLite's REAL is a
  float; storing ``996222.26328264`` there and reading it back yields a value
  that no longer compares equal to the string Aster sent, which would emit a
  phantom balance-change event on every single poll.
* **A cycle's writes land in one transaction** (:meth:`Database.commit_cycle`),
  so a crash mid-cycle cannot leave a wallet half-updated — which would either
  duplicate or lose notifications on restart.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import aiosqlite

from rpc import Balance, Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address       TEXT PRIMARY KEY,
    label         TEXT,
    added_by      INTEGER,
    added_at      INTEGER NOT NULL,
    privacy_state TEXT NOT NULL DEFAULT 'unknown',
    baselined     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wallet_balances (
    address        TEXT NOT NULL,
    asset          TEXT NOT NULL,
    wallet_balance TEXT NOT NULL,
    PRIMARY KEY (address, asset)
);

CREATE TABLE IF NOT EXISTS wallet_positions (
    address           TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    position_side     TEXT NOT NULL,
    position_amount   TEXT NOT NULL,
    entry_price       TEXT,
    unrealized_profit TEXT,
    notional_value    TEXT,
    mark_price        TEXT,
    updated_at        INTEGER NOT NULL,
    PRIMARY KEY (address, symbol, position_side)
);

-- Aster fills carry no unique trade id, so dedupe counts occurrences of an
-- identical (symbol, side, price, qty, time) tuple instead of tracking ids.
CREATE TABLE IF NOT EXISTS seen_fills (
    address    TEXT NOT NULL,
    fill_hash  TEXT NOT NULL,
    fill_time  INTEGER NOT NULL,
    seen_count INTEGER NOT NULL,
    PRIMARY KEY (address, fill_hash)
);

CREATE INDEX IF NOT EXISTS seen_fills_time ON seen_fills (fill_time);

CREATE TABLE IF NOT EXISTS fill_cursor (
    address        TEXT PRIMARY KEY,
    last_fill_time INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Wallet:
    """A tracked wallet as stored."""

    address: str
    label: str | None
    added_by: int | None
    added_at: int
    privacy_state: str
    baselined: bool

    @property
    def display(self) -> str:
        """Label if set, else the raw address."""
        return self.label or self.address


def _now_ms() -> int:
    return int(time.time() * 1000)


def normalize_address(address: str) -> str:
    """Canonical form used as the primary key everywhere."""
    return address.strip().lower()


class Database:
    """Async data-access layer over one SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the database and apply the schema idempotently."""
        parent = Path(self._path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        # WAL keeps the poller's writes from blocking /list reads.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be awaited first")
        return self._conn

    # ---------------------------------------------------------------- wallets

    async def add_wallet(
        self, address: str, label: str | None, added_by: int | None
    ) -> bool:
        """Insert a wallet. Returns False if already tracked."""
        address = normalize_address(address)
        cursor = await self.conn.execute(
            "INSERT OR IGNORE INTO wallets (address, label, added_by, added_at) "
            "VALUES (?, ?, ?, ?)",
            (address, label, added_by, _now_ms()),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def remove_wallet(self, address_or_label: str) -> str | None:
        """Remove by address or label. Returns the removed address, else None."""
        needle = address_or_label.strip()
        row = await self._fetch_one(
            "SELECT address FROM wallets WHERE address = ? OR label = ? LIMIT 1",
            (normalize_address(needle), needle),
        )
        if row is None:
            return None

        address = row["address"]
        for table in ("wallets", "wallet_balances", "wallet_positions", "seen_fills", "fill_cursor"):
            await self.conn.execute(f"DELETE FROM {table} WHERE address = ?", (address,))
        await self.conn.commit()
        return address

    async def get_wallet(self, address_or_label: str) -> Wallet | None:
        needle = address_or_label.strip()
        row = await self._fetch_one(
            "SELECT * FROM wallets WHERE address = ? OR label = ? LIMIT 1",
            (normalize_address(needle), needle),
        )
        return _row_to_wallet(row) if row else None

    async def list_wallets(self) -> list[Wallet]:
        async with self.conn.execute(
            "SELECT * FROM wallets ORDER BY added_at ASC"
        ) as cursor:
            return [_row_to_wallet(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------ last state

    async def get_balances(self, address: str) -> dict[str, Decimal]:
        async with self.conn.execute(
            "SELECT asset, wallet_balance FROM wallet_balances WHERE address = ?",
            (address,),
        ) as cursor:
            return {
                row["asset"]: Decimal(row["wallet_balance"])
                for row in await cursor.fetchall()
            }

    async def get_positions(self, address: str) -> dict[tuple[str, str], Position]:
        async with self.conn.execute(
            "SELECT * FROM wallet_positions WHERE address = ?", (address,)
        ) as cursor:
            rows = await cursor.fetchall()

        return {
            (row["symbol"], row["position_side"]): Position(
                symbol=row["symbol"],
                position_side=row["position_side"],
                position_amount=Decimal(row["position_amount"]),
                notional_value=Decimal(row["notional_value"] or "0"),
                unrealized_profit=Decimal(row["unrealized_profit"] or "0"),
                entry_price=Decimal(row["entry_price"]) if row["entry_price"] else None,
                mark_price=Decimal(row["mark_price"]) if row["mark_price"] else None,
            )
            for row in rows
        }

    async def get_seen_fill_counts(self, address: str) -> dict[str, int]:
        """Occurrence count per fill hash — the dedupe ledger."""
        async with self.conn.execute(
            "SELECT fill_hash, seen_count FROM seen_fills WHERE address = ?",
            (address,),
        ) as cursor:
            return {row["fill_hash"]: row["seen_count"] for row in await cursor.fetchall()}

    async def get_fill_cursor(self, address: str) -> int | None:
        row = await self._fetch_one(
            "SELECT last_fill_time FROM fill_cursor WHERE address = ?", (address,)
        )
        return int(row["last_fill_time"]) if row else None

    # ---------------------------------------------------------------- writes

    async def commit_cycle(
        self,
        address: str,
        *,
        balances: Sequence[Balance],
        positions: Sequence[Position],
        fill_counts: Mapping[str, tuple[int, int]],
        last_fill_time: int | None,
        privacy_state: str,
        baselined: bool = True,
    ) -> None:
        """Persist one wallet's cycle atomically.

        Args:
            fill_counts: ``{fill_hash: (count, fill_time)}`` observed this cycle;
                written as the new absolute count for that hash.
            last_fill_time: newest fill timestamp seen, or None to leave as-is.
        """
        conn = self.conn
        try:
            await conn.execute("BEGIN")

            await conn.execute(
                "UPDATE wallets SET privacy_state = ?, baselined = ? WHERE address = ?",
                (privacy_state, 1 if baselined else 0, address),
            )

            await conn.execute("DELETE FROM wallet_balances WHERE address = ?", (address,))
            await conn.executemany(
                "INSERT INTO wallet_balances (address, asset, wallet_balance) VALUES (?, ?, ?)",
                [(address, b.asset, str(b.wallet_balance)) for b in balances],
            )

            await conn.execute("DELETE FROM wallet_positions WHERE address = ?", (address,))
            now = _now_ms()
            await conn.executemany(
                "INSERT INTO wallet_positions (address, symbol, position_side, position_amount,"
                " entry_price, unrealized_profit, notional_value, mark_price, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        address,
                        p.symbol,
                        p.position_side,
                        str(p.position_amount),
                        str(p.entry_price) if p.entry_price is not None else None,
                        str(p.unrealized_profit),
                        str(p.notional_value),
                        str(p.mark_price) if p.mark_price is not None else None,
                        now,
                    )
                    for p in positions
                ],
            )

            if fill_counts:
                await conn.executemany(
                    "INSERT INTO seen_fills (address, fill_hash, fill_time, seen_count)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(address, fill_hash) DO UPDATE SET seen_count = excluded.seen_count",
                    [
                        (address, fill_hash, fill_time, count)
                        for fill_hash, (count, fill_time) in fill_counts.items()
                    ],
                )

            if last_fill_time is not None:
                await conn.execute(
                    "INSERT INTO fill_cursor (address, last_fill_time) VALUES (?, ?)"
                    " ON CONFLICT(address) DO UPDATE SET last_fill_time = excluded.last_fill_time",
                    (address, last_fill_time),
                )

            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    async def set_privacy_state(self, address: str, privacy_state: str) -> None:
        await self.conn.execute(
            "UPDATE wallets SET privacy_state = ? WHERE address = ?",
            (privacy_state, address),
        )
        await self.conn.commit()

    async def prune_seen_fills(self, older_than_ms: int) -> int:
        """Drop dedupe rows outside the API's 7-day fills window."""
        cursor = await self.conn.execute(
            "DELETE FROM seen_fills WHERE fill_time < ?", (older_than_ms,)
        )
        await self.conn.commit()
        return cursor.rowcount

    async def _fetch_one(self, sql: str, params: Iterable[object]) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, tuple(params)) as cursor:
            return await cursor.fetchone()


def _row_to_wallet(row: aiosqlite.Row) -> Wallet:
    return Wallet(
        address=row["address"],
        label=row["label"],
        added_by=row["added_by"],
        added_at=row["added_at"],
        privacy_state=row["privacy_state"],
        baselined=bool(row["baselined"]),
    )
