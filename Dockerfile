# ---- build stage: install deps into a throwaway venv ------------------------
# Keeping pip and any build wheels in this stage means none of it ships in the
# final image — only the finished venv is copied forward.
FROM python:3.13-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---- final stage: venv + source, unprivileged -------------------------------
FROM python:3.13-slim

# PYTHONUNBUFFERED matters more than usual here: logs are this bot's only
# observability surface, and buffered stdout makes `docker logs` lag reality.
# TZ defaults to UTC because the fills-window logic is timestamp-sensitive — a
# surprising local timezone is a real hazard, not a cosmetic one.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    DB_PATH=/data/tracker.db \
    HEARTBEAT_PATH=/data/heartbeat \
    PATH="/venv/bin:$PATH"

WORKDIR /app
COPY --from=build /venv /venv
COPY *.py ./

# Run unprivileged. /data is where the SQLite volume mounts and must be writable
# by this user — a root-owned mount is the classic way this container starts and
# then dies on the first write.
RUN useradd --create-home --uid 10001 tracker \
    && mkdir -p /data \
    && chown -R tracker:tracker /data /app
USER tracker

# All state lives here. Without a volume mounted at this path, the watchlist and
# the dedupe ledger die with the container and every wallet re-baselines.
VOLUME ["/data"]

# No EXPOSE: the bot long-polls Telegram and accepts no inbound connections.

# Fails if the poller hasn't refreshed the heartbeat recently (see
# healthcheck.py for the staleness rule), turning a wedged poll loop into an
# unhealthy container that `restart: unless-stopped` will recover. start-period
# covers the first Telegram connect + baseline before the check counts.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "healthcheck.py"]

# Exec form so python is PID 1 and receives SIGTERM directly, letting
# python-telegram-bot shut down cleanly instead of being killed after the grace
# period.
CMD ["python", "bot.py"]
