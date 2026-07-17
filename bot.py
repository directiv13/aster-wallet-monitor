"""Entrypoint: builds the Application, registers handlers, drives the poller.

The poll job reschedules itself rather than using ``run_repeating``. A cycle's
duration depends on the watchlist size and on how hard the rate limiter is
throttling, so a fixed period would eventually overlap itself; self-rescheduling
guarantees a full gap between cycles and lets the interval widen as wallets are
added.
"""

from __future__ import annotations

import logging

from telegram.error import TelegramError
from telegram.ext import AIORateLimiter, Application, CommandHandler, ContextTypes

import formatting as fmt
from config import Config, ConfigError, load_config
from db import Database
from handlers import Handlers, on_error
from poller import (
    BalanceChanged,
    Event,
    FillEvent,
    NowTracking,
    Poller,
    PositionClosed,
    PositionOpened,
    PositionResized,
    WentPrivate,
)
from rpc import AsterRpcClient, RateLimiter

logger = logging.getLogger(__name__)


def render_event(event: Event) -> str:
    """Map a poller event to its Telegram message body."""
    match event:
        case FillEvent():
            return fmt.format_fill(event.wallet, event.fill)
        case PositionOpened():
            return fmt.format_position_opened(event.wallet, event.position)
        case PositionClosed():
            return fmt.format_position_closed(event.wallet, event.position)
        case PositionResized():
            return fmt.format_position_resized(event.wallet, event.old, event.new)
        case BalanceChanged():
            return fmt.format_balance_changed(
                event.wallet, event.asset, event.old, event.new
            )
        case WentPrivate():
            return fmt.format_went_private(event.wallet)
        case NowTracking():
            return fmt.format_now_tracking(event.wallet, event.snapshot)
        case _:
            raise ValueError(f"no renderer for {type(event).__name__}")


def make_dispatch(application: Application, chat_id: int):
    """Build the callback the poller uses to publish events.

    A failed send is logged and swallowed: losing one notification is bad, but
    killing the poller would lose all of them.
    """

    async def dispatch(events) -> None:
        for event in events:
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=render_event(event),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except TelegramError:
                logger.exception("failed to deliver %s", type(event).__name__)
            except Exception:
                logger.exception("failed to render %s", type(event).__name__)

    return dispatch


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """One poll cycle, then reschedule itself."""
    poller: Poller = context.bot_data["poller"]
    db: Database = context.bot_data["db"]

    try:
        await poller.run_cycle()
    except Exception:  # the loop outlives any single cycle
        logger.exception("poll cycle failed")
    finally:
        try:
            wallets = await db.list_wallets()
            delay = poller.effective_interval(len(wallets))
        except Exception:
            logger.exception("could not compute next interval; using configured value")
            delay = context.bot_data["config"].poll_interval_seconds

        if context.job_queue:
            context.job_queue.run_once(poll_job, delay, name="poll")


async def post_init(application: Application) -> None:
    db: Database = application.bot_data["db"]
    await db.connect()
    logger.info("database ready at %s", application.bot_data["config"].db_path)
    if application.job_queue:
        application.job_queue.run_once(poll_job, 1, name="poll")


async def post_shutdown(application: Application) -> None:
    await application.bot_data["rpc"].aclose()
    await application.bot_data["db"].close()
    logger.info("shut down cleanly")


def build_application(config: Config) -> Application:
    """Wire the whole object graph together."""
    db = Database(config.db_path)
    # One limiter shared by poller and /list, so command traffic and background
    # polling draw on the same budget rather than racing for the IP's quota.
    limiter = RateLimiter(config.max_requests_per_second)
    rpc = AsterRpcClient(
        config.rpc_base_url,
        rate_limiter=limiter,
        timeout=config.request_timeout_seconds,
    )

    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        # Telegram caps group messages at ~20/minute; without this a busy cycle
        # would get 429'd by Telegram itself and drop notifications.
        .rate_limiter(AIORateLimiter())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    handlers = Handlers(config, db, rpc)
    application.add_handler(CommandHandler(["start", "help"], handlers.start))
    application.add_handler(CommandHandler("add", handlers.add))
    application.add_handler(CommandHandler("remove", handlers.remove))
    application.add_handler(CommandHandler("list", handlers.list_wallets))
    application.add_error_handler(on_error)

    poller = Poller(config, db, rpc, make_dispatch(application, config.target_chat_id))

    application.bot_data.update(
        {"config": config, "db": db, "rpc": rpc, "poller": poller}
    )
    return application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        config = load_config()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    logger.info(
        "starting: interval=%ss epsilon=%s rpc=%s",
        config.poll_interval_seconds,
        config.balance_change_epsilon,
        config.rpc_base_url,
    )
    application = build_application(config)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
