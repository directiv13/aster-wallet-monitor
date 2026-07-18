"""Environment-backed configuration, validated once at startup.

Loading is deliberately fail-fast: a bot that starts with a bad chat id or an
unparseable admin list would appear healthy while silently never delivering a
notification, which is far worse than refusing to boot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_RPC_BASE_URL = "https://tapi.asterdex.com/info"

# Aster rejects a userFills window wider than 7 days with a JSON-RPC error, and
# ignores `from` values below this floor. Both are enforced when building the
# fills window in poller.py.
FILLS_MAX_RANGE_MS = 7 * 24 * 60 * 60 * 1000
FILLS_MIN_FROM_MS = 1772678119418


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Config:
    """Fully validated runtime configuration."""

    telegram_bot_token: str
    target_chat_id: int
    telegram_channel_thread_id: int
    admin_user_ids: frozenset[int]
    poll_interval_seconds: float
    balance_change_epsilon: Decimal
    rpc_base_url: str
    db_path: Path
    heartbeat_path: Path
    max_requests_per_second: float
    fill_overlap_seconds: int
    request_timeout_seconds: float
    ban_cooldown_seconds: float


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ConfigError(
            f"{name} is required. Copy .env.example to .env and fill it in."
        )
    return value


def _int(name: str, default: int | None = None) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"{name} is required.")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc


def _float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}.")
    return value


def _decimal(name: str, default: str) -> Decimal:
    raw = (os.getenv(name) or "").strip() or default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a decimal number, got {raw!r}.") from exc
    if value < 0:
        raise ConfigError(f"{name} must not be negative, got {value}.")
    return value


def _admin_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"ADMIN_USER_IDS must be comma-separated integers; {chunk!r} is not one."
            ) from exc
    if not ids:
        raise ConfigError(
            "ADMIN_USER_IDS is empty — nobody could add or remove wallets."
        )
    return frozenset(ids)


def load_config(env_file: str | os.PathLike[str] | None = ".env") -> Config:
    """Read and validate configuration from the environment (and `.env`)."""
    if env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file)

    token = _require("TELEGRAM_BOT_TOKEN")
    # Group/channel ids are negative; only a plain integer is meaningful here.
    chat_id = _int("TARGET_CHAT_ID")
    channel_thread_id = _int("TELEGRAM_CHANNEL_THREAD_ID", default=0)
    admins = _admin_ids(_require("ADMIN_USER_IDS"))

    db_path = Path((os.getenv("DB_PATH") or "").strip() or "tracker.db")
    # Liveness file the poller refreshes each cycle and the container HEALTHCHECK
    # reads. Defaults beside the DB so it lands on the same persistent volume.
    heartbeat_raw = (os.getenv("HEARTBEAT_PATH") or "").strip()
    heartbeat_path = Path(heartbeat_raw) if heartbeat_raw else db_path.parent / "heartbeat"

    return Config(
        telegram_bot_token=token,
        target_chat_id=chat_id,
        telegram_channel_thread_id=channel_thread_id,
        admin_user_ids=admins,
        poll_interval_seconds=_float("POLL_INTERVAL_SECONDS", 15.0, minimum=1.0),
        balance_change_epsilon=_decimal("BALANCE_CHANGE_EPSILON", "0.01"),
        rpc_base_url=(os.getenv("RPC_BASE_URL") or "").strip() or DEFAULT_RPC_BASE_URL,
        db_path=db_path,
        heartbeat_path=heartbeat_path,
        max_requests_per_second=_float("MAX_REQUESTS_PER_SECOND", 2.0, minimum=0.1),
        fill_overlap_seconds=_int("FILL_OVERLAP_SECONDS", 120),
        request_timeout_seconds=_float("REQUEST_TIMEOUT_SECONDS", 15.0, minimum=1.0),
        ban_cooldown_seconds=_float("BAN_COOLDOWN_SECONDS", 300.0, minimum=1.0),
    )
