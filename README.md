# AsterDEX wallet tracker

A Telegram bot that watches AsterDEX perpetuals wallets and posts to a channel or
group when something happens: a new fill, a position opened/closed/resized, or a
balance move.

One shared watchlist. Admins add and remove wallets; anyone can read it. State
lives in SQLite, so a restart doesn't replay old notifications. Data comes only
from Aster's public JSON-RPC endpoint — **no API key exists or is needed**.

```
🔼 Increased LONG AKEUSDT
whale1 · 0x1e1a…714e
size 1,525,961,628 → 2,000,000,000 (+474,038,372)
$1,330,060,000.00 · entry 0.0007289203
PnL 📉 -$120,000.50
```

## Commands

| Command | Who | What |
|---|---|---|
| `/add <address> [label]` | admins | Track a wallet. Validates the address, rejects duplicates. |
| `/remove <address\|label>` | admins | Stop tracking and forget its state. |
| `/list` | anyone | Every tracked wallet: balances, open positions, entry/mark/PnL. |
| `/list <address\|label>` | anyone | One wallet in detail, including open orders. |
| `/start`, `/help` | anyone | What the bot does. |

## Setup

### 1. Create the bot

Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts. It
replies with a token like `123456789:AAE...`. That's `TELEGRAM_BOT_TOKEN`.

If the bot will post to a **group**, also send BotFather `/setprivacy` → select
your bot → **Disable**. Otherwise it can't see commands sent in the group.

### 2. Get your admin user id

Message [@userinfobot](https://t.me/userinfobot). It replies with your numeric
id. That goes in `ADMIN_USER_IDS` (comma-separated for several admins).

### 3. Get the chat id

**Group:** add the bot to the group, send any message there, then open:

```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Find `"chat":{"id":-1001234567890,...}`. Group and supergroup ids are
**negative** — include the minus sign.

**Channel:** add the bot as an **administrator** with permission to post, send a
message in the channel, and use the same `getUpdates` URL. Look for
`channel_post.chat.id`.

If `getUpdates` returns an empty list, send another message and retry — it only
returns recent, unconsumed updates. Stop the bot first if it's already running,
since it consumes updates itself.

### 4. Configure

```bash
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN`, `TARGET_CHAT_ID` and `ADMIN_USER_IDS`. Everything
else has a sensible default; see the comments in `.env.example`.

## Running

### Docker (recommended)

```bash
docker compose up -d --build
docker compose logs -f
```

State persists in the named volume `aster-tracker-data`, mounted at `/data`.
**This is what makes restarts safe** — without it every restart would re-baseline
each wallet and lose the deduplication ledger.

```bash
docker compose down          # stop; state survives
docker compose down -v       # stop AND delete all state — wallets, dedupe, everything
```

Back up the database with:

```bash
docker run --rm -v aster-tracker-data:/data -v "$PWD:/backup" \
  alpine cp /data/tracker.db /backup/tracker-backup.db
```

Keep the volume on local disk. SQLite's WAL mode needs working POSIX file
locking, which NFS and CIFS shares don't reliably provide.

### Local Python

Requires Python 3.11+ (developed on 3.13).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

On Windows, if console output dies with `UnicodeEncodeError`, the terminal is
using cp1252 and can't render the emoji in log output:

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

### Check it works without a token

```bash
python smoke.py                  # or: python smoke.py 0xYourWallet
```

Read-only. Hits the live endpoint a handful of times and prints a `/list` card
plus a price-derivation check.

## How notifications work

Each cycle (15s by default) the bot fetches each wallet's balances and positions,
plus recent fills, and diffs them against what it last saw.

- **New fill** — reported once, with symbol, side, qty, price and time.
- **Position opened / closed / resized** — keyed by symbol and side.
- **Balance change** — per asset, ignoring moves under `BALANCE_CHANGE_EPSILON`.
- **Wallet went private** — a one-time notice.

A newly added wallet is **baselined silently**: its existing positions are
recorded, not announced, so adding a whale doesn't fire thirty "opened" messages.
You get a single "now tracking" summary instead.

## Known Aster API quirks

Verified against the live endpoint. Several contradict Aster's published docs, so
they're worth knowing before changing anything:

- **Entry and mark price are not returned.** The docs list `entryPrice` and
  `markPrice`, but live responses omit them (along with `leverage`, `isolated`
  and `marginValue`). The bot derives both from `notionalValue`,
  `positionAmount` and `unrealizedProfit`, and uses the API's values if they ever
  reappear. Derivation is accurate to ~1e-13 relative — far finer than displayed
  precision — but it is a derivation, not a quote.
- **Errors come back as HTTP 200** with a JSON-RPC `error` member.
- **Empty collections are omitted**, not returned as `[]`.
- **Privacy mode withholds balances and positions entirely**, which looks exactly
  like a wallet that closed everything. The bot never diffs a private wallet, so
  going private doesn't fire a storm of false "closed" alerts.
- **A never-used wallet also reports `accountPrivacy: "enabled"`**, so "private"
  and "doesn't exist" are indistinguishable. A typo'd address just looks quiet.
- **Addresses aren't validated** — `0xdeadbeef` returns HTTP 200 and a result.
  `/add` validates the format itself.
- **Fills have no unique id.** Dedupe counts occurrences of identical
  `(symbol, side, price, qty, time)` tuples, which is what distinguishes a sliced
  order's five identical fills from one fill seen five times.

## Rate limits

Aster publishes no rate limit for this endpoint and bans abusive IPs outright
(HTTP 429 → back off; repeated abuse → HTTP 418 ban). The bot therefore:

- paces every request through one shared token bucket
  (`MAX_REQUESTS_PER_SECOND`, default 2), covering both polling and `/list`;
- backs off exponentially on 429, honouring `Retry-After`;
- stands the whole poller down for `BAN_COOLDOWN_SECONDS` on a 418;
- **widens the poll interval automatically** when the watchlist grows — 50
  wallets cost 100 calls per cycle, which will not fit in 15 seconds at a safe
  pace. It logs when it does this.

Tuned for up to ~50 wallets. Beyond that, expect the effective interval to be
dictated by the rate ceiling rather than `POLL_INTERVAL_SECONDS`.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

105 tests, no network access, ~2s. Rate-limit handling is tested against a mock
transport — **never probe the live endpoint for its limit**, that's exactly what
earns a ban.

## Project layout

```
bot.py          entrypoint: Application, handlers, self-rescheduling poll job
rpc.py          async Aster JSON-RPC client, retry/backoff, normalisation
db.py           SQLite schema and data access
poller.py       poll cycle, pure diff logic, dispatch
handlers.py     /add /remove /list /start /help + admin guard
formatting.py   message, number and address formatting
config.py       env loading and validation
smoke.py        read-only live check
```
