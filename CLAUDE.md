# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

A Telegram bot tracking AsterDEX perpetuals wallets. Polls Aster's public
JSON-RPC endpoint, diffs each wallet against stored state, posts notifications.
Async throughout (`python-telegram-bot` v21, `httpx`, `aiosqlite`).

```
bot.py        entrypoint: Application, handler registration, self-rescheduling poll job
rpc.py        Aster JSON-RPC client: retry/backoff, dataclass normalisation, price derivation
db.py         SQLite schema + data access
poller.py     poll cycle + PURE diff functions + dispatch
handlers.py   commands + admin guard
formatting.py Telegram HTML + number/address formatting
config.py     env loading/validation
smoke.py      read-only live check (no token needed)
```

## Read this before changing rpc.py or poller.py

**Aster's live API contradicts Aster's own documentation.** Each item below was
verified against the live endpoint. The guards in the code look like paranoia or
dead code if you only read the vendor docs — they are not. Do not "simplify" them
away.

1. **Errors arrive as HTTP 200** with a top-level `error` member.
   `response.status_code == 200` proves nothing. `rpc._call` checks `error`
   before touching `result`, and never retries it (deterministic).

2. **Empty collections are omitted, not empty.** A wallet with no fills has no
   `fills` key at all — same for `openOrders`, `perpAssets`, `positions`. Always
   `result.get(key) or []`. `result["fills"]` raises `KeyError` on the common case.

3. **Privacy mode withholds `perpAssets` and `positions` entirely.** On the wire,
   a private wallet is byte-identical to one that closed every position and
   drained its balance. `poller._handle_private` therefore skips diffing and
   leaves stored snapshots intact. **Deleting that guard produces a false
   "closed" storm plus a bogus balance-drain alert for every position the wallet
   holds.** There is a regression test named for exactly this.

4. **`entryPrice` and `markPrice` are documented but not returned** (nor are
   `leverage`, `isolated`, `marginValue`). `rpc._derive_prices` recovers them:
   `mark = |notional| / |amount|`, `entry = mark - pnl / signed_amount`. API
   values are preferred when present. Accuracy is ~1e-13 relative, bounded by
   Aster rounding its inputs to 8dp — do not assert bit-exact equality.

5. **Addresses are not validated.** `0xdeadbeef` returns HTTP 200 with a result.
   `handlers.EVM_ADDRESS_RE` is the only guard.

6. **A never-used wallet reports `accountPrivacy: "enabled"`** with no data, so
   "private" and "doesn't exist" cannot be distinguished. Don't build logic that
   assumes otherwise.

7. **Fills have no unique trade id.** The docs say dedupe must "rely on symbol,
   time, price, and qty combination". A sliced order yields N byte-identical
   fills at one millisecond, so `poller.select_new_fills` counts occurrences per
   hash rather than tracking a cursor. The stored count **never decreases** —
   `userFills` caps at 1000 records and can return a truncated view of a window
   it previously returned whole; letting the count drop re-notifies old fills.

## Hard rules

- **Never probe the rate limit.** Aster publishes none and bans abusive IPs
  (429 → back off, 418 → ban). Rate-limit behaviour is tested with
  `httpx.MockTransport` only. Do not write a script that hammers the endpoint to
  find the ceiling — that *is* the abuse that triggers the ban.
- **Money is `Decimal`, stored as TEXT. Never float.** `walletBalance` arrives as
  a JSON *number* with more precision than a float holds; a round-trip makes
  `996222.26328264 != 996222.26328264` and emits a phantom balance-change event
  every cycle. Parse with `Decimal(str(x))`.
- **A failed fetch is not an empty wallet.** Any RPC failure must skip the wallet
  with state untouched. Committing an empty snapshot on error fires spurious
  closes and re-fires opens on recovery.
- **Escape everything interpolated into a message** via `formatting.esc`. Labels
  are user-supplied; one unescaped `<` makes Telegram reject the whole send.
- **Keep the diff functions pure.** `diff_positions`, `diff_balances`,
  `select_new_fills` and `fills_window` take state and return events — no I/O.
  That is why the notification rules are testable without mocks.

## Conventions

- Position identity is `(symbol, position_side)`. Zero-size positions are dropped
  during normalisation so the diff reads them as closed.
- Newly added wallets are **baselined silently** (`wallets.baselined`), which is
  what distinguishes "never polled" from "polled, holds nothing".
- Telegram HTML parse mode, not MarkdownV2 (18 escape-sensitive characters,
  including `.` and `-`, which appear in every price).
- `fills_window` must stay ≤ 7 days and ≥ `FILLS_MIN_FROM_MS`; a wider range is a
  hard JSON-RPC error.

## Commands

```bash
python -m pytest              # 105 tests, no network, ~2s
python smoke.py               # read-only live check against the real endpoint
python bot.py                 # needs .env
docker compose up -d --build
```

On Windows, set `PYTHONIOENCODING=utf-8` before running anything that prints
message bodies — the emoji crash a cp1252 console.

## Gotchas

- `python-telegram-bot` needs the `[job-queue,rate-limiter]` extras; bare PTB has
  no `JobQueue` and no flood control. Telegram caps group messages at ~20/min.
- The poll job **reschedules itself** (`bot.poll_job`) instead of using
  `run_repeating`, because cycle duration varies with watchlist size and rate
  limiting; a fixed period would overlap itself.
- Under Docker the SQLite file must be on the `/data` volume (`DB_PATH` is forced
  in `docker-compose.yml`). In the container's writable layer, every rebuild
  silently wipes the watchlist and re-baselines everything.
