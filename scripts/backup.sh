#!/usr/bin/env bash
#
# Online backup of the tracker's SQLite database while the bot keeps running.
#
# Uses SQLite's own backup API (via the Python already in the container) rather
# than copying the file: a plain `cp` of a live WAL database can capture a torn,
# unrecoverable snapshot. `.backup` produces a consistent copy under concurrent
# writes.
#
# Run from the repo directory (where docker-compose.yml lives), as the deploy
# user, e.g. from cron. Keeps the last $KEEP daily copies.
#
set -euo pipefail

SERVICE="tracker"
KEEP="${KEEP:-14}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
DB_IN_CONTAINER="/data/tracker.db"
TMP_IN_CONTAINER="/data/backup-tmp.db"

cd "$(dirname "$0")"
mkdir -p "$BACKUP_DIR"

stamp="$(date -u +%Y%m%d-%H%M%S)"
out="$BACKUP_DIR/tracker-${stamp}.db"

# Consistent online snapshot inside the container, onto the same volume.
docker compose exec -T "$SERVICE" python -c "
import sqlite3
src = sqlite3.connect('${DB_IN_CONTAINER}')
dst = sqlite3.connect('${TMP_IN_CONTAINER}')
with dst:
    src.backup(dst)
dst.close(); src.close()
"

# Copy the snapshot out to the host, then remove the in-volume temp.
docker compose cp "${SERVICE}:${TMP_IN_CONTAINER}" "$out"
docker compose exec -T "$SERVICE" rm -f "${TMP_IN_CONTAINER}"

echo "Backup written: $out ($(du -h "$out" | cut -f1))"

# Retention: keep the newest $KEEP, delete the rest.
ls -1t "$BACKUP_DIR"/tracker-*.db 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    echo "Pruning old backup: $old"
    rm -f "$old"
done
