from __future__ import annotations

import logging
from datetime import time as dtime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes

from io import BytesIO

from bot.db import get_or_create_user, session_scope
from bot.models import User
from bot.reports import build_csv, build_period_report, count_on_date, local_today

logger = logging.getLogger(__name__)


def _job_name(telegram_id: int) -> str:
    return f"remind_{telegram_id}"


def parse_hhmm(text: str) -> str | None:
    raw = text.strip()
    try:
        if ":" in raw:
            hour_s, minute_s = raw.split(":", 1)
            hour = int(hour_s)
            minute = int(minute_s)
        else:
            hour = int(raw)
            minute = 0
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def schedule_user_reminder(
    application: Application,
    telegram_id: int,
    remind_at: str,
    timezone_name: str,
) -> None:
    job_queue = application.job_queue
    if job_queue is None:
        logger.warning("JobQueue is not available; reminders disabled")
        return

    name = _job_name(telegram_id)
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    hour, minute = (int(part) for part in remind_at.split(":", 1))
    tz = ZoneInfo(timezone_name)
    job_queue.run_daily(
        send_reminder,
        time=dtime(hour=hour, minute=minute, tzinfo=tz),
        chat_id=telegram_id,
        name=name,
        data={"telegram_id": telegram_id, "timezone": timezone_name},
    )
    logger.info("Scheduled daily reminder for %s at %s %s", telegram_id, remind_at, timezone_name)


def schedule_all_reminders(application: Application) -> None:
    with session_scope() as session:
        users = list(session.scalars(select(User)))
        snapshot = [(u.telegram_id, u.remind_at, u.timezone) for u in users]
    for telegram_id, remind_at, timezone_name in snapshot:
        schedule_user_reminder(application, telegram_id, remind_at, timezone_name)


def schedule_user_weekly_summary(
    application: Application,
    telegram_id: int,
    timezone_name: str,
) -> None:
    job_queue = application.job_queue
    if job_queue is None:
        logger.warning("JobQueue is not available; weekly summary disabled")
        return

    name = f"weekly_{telegram_id}"
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    tz = ZoneInfo(timezone_name)
    job_queue.run_daily(
        send_weekly_summary,
        time=dtime(hour=20, minute=0, tzinfo=tz),
        chat_id=telegram_id,
        name=name,
        data={"telegram_id": telegram_id, "timezone": timezone_name},
    )
    logger.info("Scheduled weekly summary for %s (%s)", telegram_id, timezone_name)


def schedule_all_weekly_summaries(application: Application) -> None:
    with session_scope() as session:
        users = list(session.scalars(select(User)))
        snapshot = [(u.telegram_id, u.timezone) for u in users]
    for telegram_id, timezone_name in snapshot:
        schedule_user_weekly_summary(application, telegram_id, timezone_name)


async def send_weekly_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job is None or job.chat_id is None:
        return
    telegram_id = int(job.chat_id)
    timezone_name = "Asia/Phnom_Penh"
    if job.data and isinstance(job.data, dict):
        timezone_name = str(job.data.get("timezone") or timezone_name)

    today = local_today(timezone_name)
    if today.weekday() != 6:  # 6 = Sunday in Python's datetime
        return

    with session_scope() as session:
        user = get_or_create_user(session, telegram_id)
        report = build_period_report(
            session, user.id, "week", user.timezone, user.currency
        )

    await context.bot.send_message(
        chat_id=telegram_id,
        text=f"<b>Weekly Summary</b>\n\n{report}",
        parse_mode="HTML",
    )


def schedule_user_backup(
    application: Application,
    telegram_id: int,
    timezone_name: str,
) -> None:
    job_queue = application.job_queue
    if job_queue is None:
        logger.warning("JobQueue is not available; daily backup disabled")
        return

    name = f"backup_{telegram_id}"
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    tz = ZoneInfo(timezone_name)
    job_queue.run_daily(
        send_daily_backup,
        time=dtime(hour=23, minute=59, tzinfo=tz),
        chat_id=telegram_id,
        name=name,
        data={"telegram_id": telegram_id, "timezone": timezone_name},
    )
    logger.info("Scheduled daily backup for %s at 23:59 %s", telegram_id, timezone_name)


def schedule_all_backups(application: Application) -> None:
    with session_scope() as session:
        users = list(session.scalars(select(User)))
        snapshot = [(u.telegram_id, u.timezone) for u in users]
    for telegram_id, timezone_name in snapshot:
        schedule_user_backup(application, telegram_id, timezone_name)


async def send_daily_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job is None or job.chat_id is None:
        return
    telegram_id = int(job.chat_id)
    timezone_name = "Asia/Phnom_Penh"
    if job.data and isinstance(job.data, dict):
        timezone_name = str(job.data.get("timezone") or timezone_name)

    with session_scope() as session:
        user = get_or_create_user(session, telegram_id)
        today = local_today(user.timezone)
        data = build_csv(session, user.id)
        tx_count = data.count(b"\n") - 1  # subtract header row

    if tx_count <= 0:
        return  # nothing to back up yet

    filename = f"backup_{today.isoformat()}.csv"
    caption = (
        f"Daily backup  —  {today.strftime('%d %b %Y')}\n"
        f"{tx_count} transaction(s) total."
    )
    await context.bot.send_document(
        chat_id=telegram_id,
        document=BytesIO(data),
        filename=filename,
        caption=caption,
    )
    logger.info("Sent daily backup to %s (%d rows)", telegram_id, tx_count)


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job is None or job.chat_id is None:
        return
    telegram_id = int(job.chat_id)
    timezone_name = "Asia/Phnom_Penh"
    if job.data and isinstance(job.data, dict):
        timezone_name = str(job.data.get("timezone") or timezone_name)

    with session_scope() as session:
        user = get_or_create_user(session, telegram_id)
        timezone_name = user.timezone
        today = local_today(timezone_name)
        logged = count_on_date(session, user.id, today)

    if logged:
        text = (
            f"Evening check-in.\n"
            f"You already logged <b>{logged}</b> transaction(s) today. "
            "Add anything else?"
        )
    else:
        text = (
            "Did you log today's money?\n"
            "Use /expense or /income, or tap a button below."
        )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Expense", callback_data="log:expense"),
                InlineKeyboardButton("Income", callback_data="log:income"),
            ],
            [InlineKeyboardButton("Today", callback_data="cmd:today")],
        ]
    )
    await context.bot.send_message(
        chat_id=telegram_id,
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )
