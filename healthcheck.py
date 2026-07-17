"""Container HEALTHCHECK: is the poll loop still turning?

The poller refreshes a heartbeat file at the end of every cycle (see
``Poller._write_heartbeat``). If that file is missing or stale, the async loop is
wedged — hung on a socket, deadlocked, whatever — and the process needs
restarting even though it is nominally alive. Docker's ``restart: unless-stopped``
does the recovering; this script is only the detector.

Exit 0 = healthy, exit 1 = unhealthy. Stdlib only, no third-party imports, so it
adds nothing to the image.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def _max_age_seconds() -> float:
    """How old the heartbeat may get before we call the poller wedged.

    Explicit ``HEARTBEAT_MAX_AGE_SECONDS`` wins. Otherwise 3x the poll interval,
    floored at 90s so a fast interval doesn't produce a hair-trigger that restarts
    the bot over a single slow cycle. Large watchlists widen the *effective*
    interval above the base value, so raise the explicit override if a big
    watchlist trips false unhealthies.
    """
    explicit = (os.getenv("HEARTBEAT_MAX_AGE_SECONDS") or "").strip()
    if explicit:
        try:
            return float(explicit)
        except ValueError:
            pass
    try:
        interval = float((os.getenv("POLL_INTERVAL_SECONDS") or "").strip() or "15")
    except ValueError:
        interval = 15.0
    return max(90.0, interval * 3)


def main() -> int:
    path = Path((os.getenv("HEARTBEAT_PATH") or "").strip() or "/data/heartbeat")
    if not path.exists():
        print(f"unhealthy: no heartbeat at {path} yet", file=sys.stderr)
        return 1

    age = time.time() - path.stat().st_mtime
    limit = _max_age_seconds()
    if age > limit:
        print(
            f"unhealthy: heartbeat {age:.0f}s old (limit {limit:.0f}s)",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
