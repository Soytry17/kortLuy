from __future__ import annotations

import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import InvalidToken
from telegram.ext import Application, ContextTypes, Defaults

from bot.config import get_settings
from bot.db import init_db
from bot.handlers import register_handlers
from bot.reminders import schedule_all_backups, schedule_all_reminders, schedule_all_weekly_summaries

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import BadRequest, Conflict
    err = context.error
    if isinstance(err, BadRequest) and "query is too old" in str(err).lower():
        return  # harmless: free-tier spin-down caused the button to expire
    if isinstance(err, Conflict):
        logger.warning("Conflict: another bot instance is running. Stop the local bot.")
        return
    logger.exception("Unhandled error while processing %s", update, exc_info=err)


def bind_platform_port() -> None:
    """Railway/Render expect a process listening on $PORT."""
    raw = os.getenv("PORT")
    if not raw:
        return

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("0.0.0.0", int(raw)), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Listening on port %s for platform health checks", raw)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Dashboard and menu"),
            BotCommand("help", "All commands"),
            BotCommand("expense", "Log an expense"),
            BotCommand("income", "Log income"),
            BotCommand("today", "Today's totals"),
            BotCommand("week", "This week's totals"),
            BotCommand("month", "This month's totals"),
            BotCommand("balance", "All-time balance"),
            BotCommand("categories", "List or edit categories"),
            BotCommand("undo", "Remove the last entry"),
            BotCommand("export", "Download CSV"),
            BotCommand("budget", "Monthly spending limits"),
            BotCommand("edit", "Edit a past transaction"),
            BotCommand("rate", "Set KHR/USD exchange rate"),
            BotCommand("report", "Custom date range report"),
            BotCommand("goals", "Savings goals"),
            BotCommand("backup", "Download a backup now"),
            BotCommand("remind", "Set daily reminder time"),
            BotCommand("cancel", "Stop the current prompt"),
        ]
    )
    schedule_all_reminders(application)
    schedule_all_weekly_summaries(application)
    schedule_all_backups(application)


def main() -> None:
    settings = get_settings()
    if not settings.bot_token:
        logger.error("BOT_TOKEN is missing. Copy .env.example to .env and add your token.")
        sys.exit(1)
    if not settings.allowed_user_ids:
        logger.warning(
            "ALLOWED_USER_IDS is empty; anyone who finds the bot can use it. "
            "Set it in production."
        )

    init_db()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    bind_platform_port()
    application = (
        Application.builder()
        .token(settings.bot_token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .post_init(post_init)
        .build()
    )
    register_handlers(application)
    application.add_error_handler(on_error)
    logger.info("Bot starting (polling)")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        logger.error(
            "BOT_TOKEN was rejected by Telegram. Copy a fresh token from BotFather into .env."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
