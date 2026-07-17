# Deploying the AsterDEX tracker bot

Runbook for running the bot on an Ubuntu 22.04/24.04 LTS VPS with Docker +
Compose v2. The bot is **outbound-only** — it long-polls Telegram (`getUpdates`)
and calls `tapi.asterdex.com` over HTTPS. There are **no inbound ports, no
webhook, no reverse proxy, and no TLS certs** to manage. All state is a single
SQLite file on a named Docker volume.

---

## 0. What you need

- A fresh Ubuntu LTS VPS and a sudo-capable login.
- A Telegram bot token (`@BotFather` → `/newbot`), the target chat id, and your
  admin user id (`@userinfobot`). See the project README for details.

---

## 1. Provision the box (once)

Copy `provision.sh` to the server and run it as root. It updates the OS,
installs Docker Engine + the Compose plugin from Docker's official repo, creates
a non-root `deploy` user in the `docker` group, enables unattended security
upgrades, turns on NTP time sync, and sets a deny-inbound UFW firewall that
allows only (rate-limited) SSH.

```bash
scp provision.sh you@server:/tmp/
ssh you@server 'sudo bash /tmp/provision.sh'      # or DEPLOY_USER=bot sudo bash ...
```

After it finishes:

```bash
# Log in as the deploy user, then confirm docker works without sudo:
docker run --rm hello-world
```

> **SSH hardening** (advisory, printed by the script): once key-based login
> works, set `PasswordAuthentication no`, `PermitRootLogin no` in
> `/etc/ssh/sshd_config` and `sudo systemctl restart ssh`. Verify you can still
> log in with your key **before** ending the session.

---

## 2. First deploy

As the **deploy user**:

```bash
git clone <repo-url> ~/aster-whale-monitor
cd ~/aster-whale-monitor

cp .env.example .env
nano .env                 # fill in TELEGRAM_BOT_TOKEN, TARGET_CHAT_ID, ADMIN_USER_IDS
chmod 600 .env            # keep secrets readable only by you

docker compose up -d --build
```

Watch it come up:

```bash
docker compose logs -f            # look for "database ready" and the poll job starting
docker compose ps                 # STATUS should become "healthy" within ~1–2 min
```

`.env` is git-ignored and excluded from the image (`.dockerignore`) — it is read
at container start via `env_file:` and never baked into a layer. `DB_PATH` and
`HEARTBEAT_PATH` are forced to `/data/...` by `docker-compose.yml`, so state
always lands on the persistent volume regardless of what `.env` says.

---

## 3. Update / redeploy

```bash
cd ~/aster-whale-monitor
git pull
docker compose up -d --build      # rebuilds, recreates; the named volume (state) is kept
```

The `aster-tracker-data` volume is not touched by a rebuild, so the watchlist,
last-seen positions/balances, and the fill-dedupe ledger survive. No duplicate
notifications after a redeploy.

---

## 4. Backups

`backup.sh` takes a **consistent online backup** (via SQLite's `.backup` API,
run through the Python already in the container) without stopping the bot, writes
it to `~/backups/tracker-YYYYmmdd-HHMMSS.db`, and prunes to the newest 14.

```bash
cd ~/aster-whale-monitor
chmod +x backup.sh
./backup.sh
```

Schedule it daily with cron (`crontab -e` as the deploy user):

```cron
30 3 * * * cd $HOME/aster-whale-monitor && ./backup.sh >> $HOME/backups/backup.log 2>&1
```

### Restore

```bash
cd ~/aster-whale-monitor
docker compose stop
# Drop the chosen backup into the volume as the live DB, and clear stale WAL/SHM:
docker compose run --rm --no-deps -v "$HOME/backups:/backups" tracker \
    sh -c 'cp /backups/tracker-YYYYmmdd-HHMMSS.db /data/tracker.db \
           && rm -f /data/tracker.db-wal /data/tracker.db-shm'
docker compose up -d
docker compose logs -f
```

Backups live in `~/backups/` on the host. The `.db` file is self-contained;
because the online backup checkpoints the WAL, you only need the single file.

---

## 5. Ops one-liners

```bash
docker compose logs -f                         # tail logs
docker compose logs --since 1h                 # recent logs
docker compose restart                         # restart the bot
docker compose stop                            # stop (state preserved)
docker compose down                            # stop + remove container (volume kept)
docker compose exec tracker sh                 # shell into the container
docker compose exec tracker cat /data/heartbeat   # last successful cycle timestamp
docker inspect --format '{{.State.Health.Status}}' aster-whale-monitor
docker volume inspect aster-whale-monitor_aster-tracker-data   # where state lives on disk
```

The container reports **healthy** only while the poll loop keeps refreshing
`/data/heartbeat`; a wedged poller goes **unhealthy** and `restart:
unless-stopped` recovers it. If a large watchlist widens the effective poll
interval past the heartbeat limit, raise `HEARTBEAT_MAX_AGE_SECONDS` in `.env`.

---

## 6. Reboot behaviour

Docker's service is enabled at boot and the service uses `restart:
unless-stopped`, so the bot comes back automatically after a VPS reboot. Nothing
to do by hand.

---

## 7. Security checklist

- [ ] `.env` is `chmod 600`, git-ignored, and never in the image.
- [ ] Container runs as non-root (`uid 10001`); volume files are owned by it.
- [ ] Auto-start on reboot (Docker service enabled + `restart: unless-stopped`).
- [ ] Log rotation on (`json-file`, `max-size 10m`, `max-file 5`) — logs can't
      fill the disk.
- [ ] UFW: inbound denied except rate-limited SSH; outbound open (Telegram +
      Aster reachable).
- [ ] Unattended security upgrades enabled; NTP time sync on.
- [ ] SSH hardened (key-only, no root/password login) once keys are confirmed.
- [ ] Graceful shutdown: `docker compose down` stops the poller cleanly (PTB
      handles SIGTERM; `init: true` forwards it).
