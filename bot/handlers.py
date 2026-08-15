from __future__ import annotations

import logging
from functools import wraps
from html import escape
from io import BytesIO
from typing import Awaitable, Callable

from sqlalchemy import select
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.charts import build_goal_chart, build_trends_chart
from bot.config import get_settings
from bot.db import (
    add_category,
    add_goal,
    add_transaction,
    category_usage_count,
    delete_goal,
    deposit_to_goal,
    find_category,
    get_or_create_user,
    get_transaction,
    income_expense_for_period,
    last_transaction,
    list_categories,
    list_goals,
    month_spending_for_category,
    recent_transactions,
    session_scope,
    total_expense_for_period,
)
from bot.models import Category, User
from bot.money import (
    convert_to_cents,
    format_khr,
    format_money,
    parse_amount_and_currency,
    parse_amount_to_cents,
)
from bot.reminders import (
    parse_hhmm,
    schedule_user_backup,
    schedule_user_reminder,
    schedule_user_weekly_summary,
)
from bot.reports import (
    build_balance_report,
    build_csv,
    build_custom_range_report,
    build_period_report,
    build_trends_report,
    local_today,
    parse_date_input,
    period_range,
    sum_by_kind,
)

AMOUNT, CATEGORY, NOTE = range(3)
EDIT_TX, EDIT_FIELD, EDIT_AMOUNT_NEW, EDIT_CAT_NEW, EDIT_NOTE_NEW = range(3, 8)
BUDGET_SET_INPUT = 8
REPORT_START, REPORT_END = 9, 10
GOAL_NAME, GOAL_AMOUNT = 11, 12
GOAL_DEPOSIT = 13
logger = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[object]]


def _is_allowed(telegram_id: int) -> bool:
    allowed = get_settings().allowed_user_ids
    if not allowed:
        return True
    return telegram_id in allowed


def allowed_only(func: Handler) -> Handler:
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or not _is_allowed(user.id):
            text = "This bot is private."
            if user is not None:
                text += f"\nYour Telegram ID is <code>{user.id}</code>."
            if update.callback_query:
                await update.callback_query.answer("This bot is private.", show_alert=True)
            elif update.message:
                await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return ConversationHandler.END
        return await func(update, context)

    return wrapper


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➖ Expense", callback_data="log:expense"),
                InlineKeyboardButton("➕ Income", callback_data="log:income"),
            ],
            [
                InlineKeyboardButton("📅 Today", callback_data="cmd:today"),
                InlineKeyboardButton("📆 Week", callback_data="cmd:week"),
                InlineKeyboardButton("🗓 Month", callback_data="cmd:month"),
            ],
            [
                InlineKeyboardButton("💳 Balance", callback_data="cmd:balance"),
                InlineKeyboardButton("🎯 Budget", callback_data="cmd:budget"),
                InlineKeyboardButton("📊 Trends", callback_data="cmd:trends"),
            ],
            [
                InlineKeyboardButton("🏦 Goals", callback_data="cmd:goals"),
                InlineKeyboardButton("📤 Export", callback_data="cmd:export"),
            ],
        ]
    )


def category_keyboard(categories: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for cat_id, name in categories:
        row.append(InlineKeyboardButton(name, callback_data=f"cat:{cat_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data="log:cancel")])
    return InlineKeyboardMarkup(rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖ Cancel", callback_data="log:cancel")]]
    )


def skip_note_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏭ Skip", callback_data="note:skip"),
                InlineKeyboardButton("✖ Cancel", callback_data="log:cancel"),
            ]
        ]
    )


def delete_category_keyboard(
    categories: list[tuple[int, str, str, bool]],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for cat_id, name, kind, is_default in categories:
        if is_default:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    f"Delete {kind}/{name}",
                    callback_data=f"delcat:{cat_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


async def reply(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    edit: bool = False,
) -> None:
    if update.callback_query:
        query = update.callback_query
        if query.message is None:
            return
        if edit:
            markup = reply_markup if reply_markup is not None else InlineKeyboardMarkup([])
            try:
                await query.edit_message_text(
                    text, reply_markup=markup, parse_mode=ParseMode.HTML
                )
                return
            except BadRequest:
                pass
        await query.message.reply_text(
            text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
        )
        return
    if update.message:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
        )


def ensure_user(telegram_id: int) -> tuple[int, str, str, str, bool]:
    with session_scope() as session:
        was_new = (
            session.scalar(select(User).where(User.telegram_id == telegram_id)) is None
        )
        user = get_or_create_user(session, telegram_id)
        return user.id, user.timezone, user.remind_at, user.currency, was_new


def _maybe_schedule(context: ContextTypes.DEFAULT_TYPE, telegram_id: int) -> None:
    _, timezone_name, remind_at, _, was_new = ensure_user(telegram_id)
    if was_new:
        schedule_user_reminder(context.application, telegram_id, remind_at, timezone_name)
        schedule_user_weekly_summary(context.application, telegram_id, timezone_name)
        schedule_user_backup(context.application, telegram_id, timezone_name)


HELP_TEXT = (
    "<b>Log</b>\n"
    "<code>/expense 4.50 food lunch</code>\n"
    "<code>/income 500 salary payday</code>\n"
    "<code>/expense 10000 KHR food</code>  ← KHR auto-converts\n\n"
    "<b>Reports</b>\n"
    "/today  •  /week  •  /month  •  /balance\n"
    "/trends — chart: this week vs last week &amp; month vs month\n"
    "/report — custom date range report\n\n"
    "<b>Budget &amp; Goals</b>\n"
    "/budget — daily / weekly / monthly spending limit\n"
    "/goals — savings goals with progress chart\n\n"
    "<b>Edit &amp; Settings</b>\n"
    "/edit — change amount, category, or note of a past entry\n"
    "/categories — list, add, delete\n"
    "/rate 4100 — set KHR per 1 USD\n"
    "/remind 21:00 — daily reminder time\n\n"
    "<b>Other</b>\n"
    "/undo — remove last entry\n"
    "/export — download CSV\n"
    "/backup — download full backup now\n\n"
    "<i>Daily backup sent at 23:59.  Weekly summary every Sunday 20:00.</i>"
)


def _build_dashboard(
    name: str,
    today_inc: int,
    today_exp: int,
    month_inc: int,
    month_exp: int,
    balance: int,
    currency: str,
    month_label: str,
    budget_today: int | None,
    budget_month: int | None,
) -> str:
    today_net = today_inc - today_exp
    today_sign = "+" if today_net >= 0 else ""
    month_net = month_inc - month_exp
    month_sign = "+" if month_net >= 0 else ""
    bal_sign = "+" if balance >= 0 else ""

    lines = [
        f"👋 <b>Hi {escape(name)}!</b>",
        "",
        f"<b>Today</b>",
    ]
    if budget_today:
        used_pct = min(int(today_exp / budget_today * 100), 100)
        bar = "█" * (used_pct // 10) + "░" * (10 - used_pct // 10)
        remaining = budget_today - today_exp
        rem_sign = "" if remaining >= 0 else "-"
        lines.append(f"  💸 Spent    <b>{format_money(today_exp, currency)}</b>  /  {format_money(budget_today, currency)}")
        lines.append(f"  {bar}  {used_pct}%  left {rem_sign}{format_money(abs(remaining), currency)}")
    else:
        lines.append(f"  💸 Spent    <b>{format_money(today_exp, currency)}</b>")
    if today_inc > 0:
        lines.append(f"  💰 Earned   <b>{format_money(today_inc, currency)}</b>")
    lines.append(f"  Net        <b>{today_sign}{format_money(today_net, currency)}</b>")

    lines += [
        "",
        f"<b>{escape(month_label)}</b>",
    ]
    if budget_month:
        used_pct = min(int(month_exp / budget_month * 100), 100)
        bar = "█" * (used_pct // 10) + "░" * (10 - used_pct // 10)
        remaining = budget_month - month_exp
        rem_sign = "" if remaining >= 0 else "-"
        lines.append(f"  💸 Spent    <b>{format_money(month_exp, currency)}</b>  /  {format_money(budget_month, currency)}")
        lines.append(f"  {bar}  {used_pct}%  left {rem_sign}{format_money(abs(remaining), currency)}")
    else:
        lines.append(f"  💸 Spent    <b>{format_money(month_exp, currency)}</b>")
    lines.append(f"  💰 Earned   <b>{format_money(month_inc, currency)}</b>")
    lines.append(f"  Net        <b>{month_sign}{format_money(month_net, currency)}</b>")

    lines += [
        "",
        f"💳 <b>All-time balance</b>  <b>{bal_sign}{format_money(balance, currency)}</b>",
        "",
        "<i>Tap a button below to get started.</i>",
    ]
    return "\n".join(lines)


@allowed_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.effective_user is not None
    tg_user = update.effective_user
    _maybe_schedule(context, tg_user.id)

    with session_scope() as session:
        user = get_or_create_user(session, tg_user.id)
        today = local_today(user.timezone)
        month_start, month_end = period_range("month", user.timezone)

        today_inc, today_exp = income_expense_for_period(session, user.id, today, today)
        month_inc, month_exp = income_expense_for_period(session, user.id, month_start, month_end)
        all_inc, all_exp = sum_by_kind(session, user.id)
        balance = all_inc - all_exp

        currency = user.currency
        budget_today = user.budget_today_cents
        budget_month = user.budget_month_cents
        month_label = month_end.strftime("%B %Y")

    first_name = tg_user.first_name or "there"
    text = _build_dashboard(
        first_name,
        today_inc, today_exp,
        month_inc, month_exp,
        balance,
        currency,
        month_label,
        budget_today,
        budget_month,
    )
    await reply(update, text, main_keyboard())
    return ConversationHandler.END


@allowed_only
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await reply(update, HELP_TEXT, main_keyboard())
    return ConversationHandler.END


@allowed_only
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await reply(update, "Cancelled.", main_keyboard(), edit=True)
    else:
        await reply(update, "Cancelled.", main_keyboard())
    return ConversationHandler.END


@allowed_only
async def expense_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await begin_log(update, context, "expense")


@allowed_only
async def income_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await begin_log(update, context, "income")


@allowed_only
async def start_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    kind = query.data.split(":", 1)[1]
    context.user_data.clear()
    context.user_data["kind"] = kind
    prompt = "How much did you spend?" if kind == "expense" else "How much did you receive?"
    await reply(update, prompt, cancel_keyboard(), edit=True)
    return AMOUNT


async def begin_log(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> int:
    assert update.effective_user is not None
    logger.info("begin_log kind=%s user=%s args=%s", kind, update.effective_user.id, context.args)
    _maybe_schedule(context, update.effective_user.id)
    context.user_data.clear()
    context.user_data["kind"] = kind
    arg_text = " ".join(context.args or []).strip()
    if not arg_text:
        prompt = (
            "How much did you spend?"
            if kind == "expense"
            else "How much did you receive?"
        )
        await reply(update, prompt, cancel_keyboard())
        return AMOUNT
    return await _apply_amount_and_rest(update, context, arg_text)


async def _apply_amount_and_rest(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> int:
    assert update.effective_user is not None
    kind = str(context.user_data.get("kind"))
    parts = text.split()

    parsed = parse_amount_and_currency(parts[0])
    if parsed is None:
        await reply(
            update,
            "I couldn't read that amount. Try like <code>4.50</code> or <code>10000 KHR</code>.",
        )
        return AMOUNT

    raw_amount, currency = parsed
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        cents = convert_to_cents(raw_amount, currency, user.khr_rate)

        if currency == "KHR":
            context.user_data["original_display"] = format_khr(raw_amount)
        else:
            context.user_data.pop("original_display", None)

        context.user_data["amount_cents"] = cents
        rest = parts[1:]
        if rest:
            category = find_category(session, rest[0], kind)
            if category:
                note = " ".join(rest[1:]) or None
                return await _save_transaction(update, context, session, category.id, note)
            context.user_data["pending_note"] = " ".join(rest)
        choices = [(c.id, c.name) for c in list_categories(session, kind)]

    amount_label = format_money(cents, user.currency)
    if currency == "KHR":
        amount_label += f" ({context.user_data['original_display']})"

    hint = ""
    if context.user_data.get("pending_note"):
        hint = (
            f"\nUnknown category <code>{escape(str(context.user_data['pending_note']).split()[0])}</code>. "
            "Pick one below — leftover text will be saved as the note."
        )
    await reply(
        update,
        f"Amount: <b>{amount_label}</b>\nPick a category.{hint}",
        category_keyboard(choices),
    )
    return CATEGORY


@allowed_only
async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    return await _apply_amount_and_rest(update, context, update.message.text.strip())


@allowed_only
async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    category_id = int(query.data.split(":", 1)[1])
    context.user_data["category_id"] = category_id
    pending = context.user_data.get("pending_note")
    if pending:
        with session_scope() as session:
            return await _save_transaction(
                update, context, session, category_id, str(pending)
            )
    await reply(
        update,
        "Add a note? Send text or tap Skip.",
        skip_note_keyboard(),
        edit=True,
    )
    return NOTE


@allowed_only
async def category_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    kind = str(context.user_data.get("kind"))
    name = update.message.text.strip()
    with session_scope() as session:
        category = find_category(session, name, kind)
        if category is None:
            choices = [(c.id, c.name) for c in list_categories(session, kind)]
            await reply(
                update,
                f"No category named <code>{escape(name)}</code>. Pick one below.",
                category_keyboard(choices),
            )
            return CATEGORY
        context.user_data["category_id"] = category.id
        pending = context.user_data.get("pending_note")
        if pending:
            return await _save_transaction(
                update, context, session, category.id, str(pending)
            )
    await reply(update, "Add a note? Send text or tap Skip.", skip_note_keyboard())
    return NOTE


@allowed_only
async def note_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    category_id = int(context.user_data["category_id"])
    with session_scope() as session:
        return await _save_transaction(
            update, context, session, category_id, update.message.text
        )


@allowed_only
async def note_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    category_id = int(context.user_data["category_id"])
    with session_scope() as session:
        return await _save_transaction(update, context, session, category_id, None)


async def _save_transaction(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session,
    category_id: int,
    note: str | None,
) -> int:
    assert update.effective_user is not None
    kind = str(context.user_data["kind"])
    cents = int(context.user_data["amount_cents"])
    user = get_or_create_user(session, update.effective_user.id)
    category = session.get(Category, category_id)
    cat_name = category.name if category else "uncategorized"
    tx = add_transaction(
        session,
        user_id=user.id,
        kind=kind,
        amount_cents=cents,
        category_id=category_id,
        note=note,
        occurred_on=local_today(user.timezone),
    )
    sign = "+" if kind == "income" else "-"
    note_bit = f"\nNote: {escape(tx.note)}" if tx.note else ""
    original_bit = ""
    if context.user_data.get("original_display"):
        original_bit = f"  <i>({context.user_data['original_display']})</i>"

    budget_warning = ""
    if kind == "expense":
        period_limits = {
            "today": user.budget_today_cents,
            "week": user.budget_week_cents,
            "month": user.budget_month_cents,
        }
        period_names = {"today": "Daily", "week": "Weekly", "month": "Monthly"}
        for chk_period, limit in period_limits.items():
            if not limit or limit <= 0:
                continue
            start, end = period_range(chk_period, user.timezone)
            spent = total_expense_for_period(session, user.id, start, end)
            pct = int(spent / limit * 100)
            plabel = period_names[chk_period]
            if spent >= limit:
                budget_warning += (
                    f"\n\n<b>{plabel} budget exceeded!</b> "
                    f"{format_money(spent, user.currency)} / {format_money(limit, user.currency)} ({pct}%)"
                )
            elif pct >= 80:
                budget_warning += (
                    f"\n\n{plabel} budget at {pct}%: "
                    f"{format_money(spent, user.currency)} / {format_money(limit, user.currency)}"
                )

    msg = (
        f"Logged {kind}: <b>{sign}{format_money(cents, user.currency)}</b>{original_bit}\n"
        f"Category: {escape(cat_name)}{note_bit}{budget_warning}"
    )
    context.user_data.clear()
    await reply(
        update, msg, main_keyboard(), edit=bool(update.callback_query)
    )
    return ConversationHandler.END


@allowed_only
async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_period(update, context, "today")


@allowed_only
async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_period(update, context, "week")


@allowed_only
async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_period(update, context, "month")


async def _send_period(
    update: Update, context: ContextTypes.DEFAULT_TYPE, period: str
) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    assert update.effective_user is not None
    _maybe_schedule(context, update.effective_user.id)
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        text = build_period_report(
            session, user.id, period, user.timezone, user.currency
        )
    await reply(update, text, main_keyboard())


@allowed_only
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    assert update.effective_user is not None
    _maybe_schedule(context, update.effective_user.id)
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        text = build_balance_report(session, user.id, user.currency)
    await reply(update, text, main_keyboard())


@allowed_only
async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user is not None
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        tx = last_transaction(session, user.id)
        if tx is None:
            await reply(update, "Nothing to undo.")
            return
        cat = tx.category.name if tx.category else "uncategorized"
        amount = format_money(tx.amount_cents, user.currency)
        kind = tx.kind
        session.delete(tx)
    await reply(
        update,
        f"Removed last {escape(kind)}: {amount} ({escape(cat)}).",
        main_keyboard(),
    )


@allowed_only
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    assert update.effective_user is not None
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        data = build_csv(session, user.id)
        count = data.count(b"\n") - 1
    if count <= 0:
        await reply(update, "No transactions to export yet.")
        return
    document = InputFile(BytesIO(data), filename="transactions.csv")
    caption = f"{count} transaction(s)"
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_document(document=document, caption=caption)
    elif update.message:
        await update.message.reply_document(document=document, caption=caption)


@allowed_only
async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a CSV backup of all transactions on demand."""
    if update.callback_query:
        await update.callback_query.answer()
    assert update.effective_user is not None
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        from bot.reports import local_today, build_csv
        today = local_today(user.timezone)
        data = build_csv(session, user.id)
        tx_count = data.count(b"\n") - 1

    if tx_count <= 0:
        await reply(update, "No transactions to back up yet.")
        return

    from io import BytesIO
    filename = f"backup_{today.isoformat()}.csv"
    caption = (
        f"Backup  —  {today.strftime('%d %b %Y')}\n"
        f"{tx_count} transaction(s) total."
    )
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_document(
            document=BytesIO(data), filename=filename, caption=caption
        )
    elif update.message:
        await update.message.reply_document(
            document=BytesIO(data), filename=filename, caption=caption
        )


@allowed_only
async def categories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0].lower() == "add":
        await _categories_add(update, args[1:])
        return
    if args and args[0].lower() in {"del", "delete", "rm"}:
        await _categories_delete_by_name(update, args[1:])
        return
    await _categories_list(update)


async def _categories_list(update: Update) -> None:
    with session_scope() as session:
        snapshot = [
            (c.id, c.name, c.kind, c.is_default) for c in list_categories(session)
        ]
    expense = [row for row in snapshot if row[2] == "expense"]
    income = [row for row in snapshot if row[2] == "income"]

    def fmt(items: list[tuple[int, str, str, bool]]) -> str:
        if not items:
            return "  (none)"
        return "\n".join(
            f"  {escape(name)}" + ("" if is_default else " (custom)")
            for _id, name, _kind, is_default in items
        )

    text = (
        "<b>Categories</b>\n\n"
        f"<b>Expense</b>\n{fmt(expense)}\n\n"
        f"<b>Income</b>\n{fmt(income)}\n\n"
        "Add: <code>/categories add expense coffee</code>\n"
        "Delete: tap a custom category below, or "
        "<code>/categories del expense coffee</code>"
    )
    await reply(update, text, delete_category_keyboard(snapshot))


async def _categories_add(update: Update, args: list[str]) -> None:
    if len(args) < 2:
        await reply(
            update,
            "Usage: <code>/categories add expense coffee</code> "
            "or <code>/categories add income bonus</code>",
        )
        return
    kind = args[0].lower()
    if kind not in {"expense", "income"}:
        await reply(update, "Kind must be <code>expense</code> or <code>income</code>.")
        return
    name = " ".join(args[1:]).strip().lower()
    if not name or len(name) > 64:
        await reply(update, "Give a short category name.")
        return
    with session_scope() as session:
        if find_category(session, name, kind):
            await reply(update, f"<code>{escape(name)}</code> already exists for {kind}.")
            return
        add_category(session, name, kind)
    await reply(update, f"Added {kind} category <code>{escape(name)}</code>.")


async def _categories_delete_by_name(update: Update, args: list[str]) -> None:
    if len(args) < 2:
        await reply(
            update,
            "Usage: <code>/categories del expense coffee</code>",
        )
        return
    kind = args[0].lower()
    name = " ".join(args[1:]).strip()
    with session_scope() as session:
        category = find_category(session, name, kind)
        if category is None:
            await reply(update, "No such category.")
            return
        await _delete_category(update, session, category)


async def _delete_category(update: Update, session, category: Category) -> None:
    used = category_usage_count(session, category.id)
    if used:
        await reply(
            update,
            f"Can't delete <code>{escape(category.name)}</code> — "
            f"used in {used} transaction(s).",
        )
        return
    name, kind = category.name, category.kind
    session.delete(category)
    await reply(update, f"Deleted {escape(kind)} category <code>{escape(name)}</code>.")


@allowed_only
async def delete_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    category_id = int(query.data.split(":", 1)[1])
    with session_scope() as session:
        category = session.get(Category, category_id)
        if category is None:
            await reply(update, "That category is already gone.")
            return
        await _delete_category(update, session, category)


@allowed_only
async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user is not None
    telegram_id = update.effective_user.id
    args = context.args or []
    with session_scope() as session:
        user = get_or_create_user(session, telegram_id)
        if not args:
            await reply(
                update,
                f"Daily reminder is <b>{escape(user.remind_at)}</b> "
                f"({escape(user.timezone)}).\n"
                "Change it with <code>/remind 21:00</code>.",
            )
            return
        parsed = parse_hhmm(" ".join(args))
        if parsed is None:
            await reply(update, "Use 24-hour time like <code>/remind 21:00</code>.")
            return
        user.remind_at = parsed
        timezone_name = user.timezone
        remind_at = user.remind_at
    schedule_user_reminder(context.application, telegram_id, remind_at, timezone_name)
    await reply(
        update,
        f"Daily reminder set to <b>{escape(remind_at)}</b> ({escape(timezone_name)}).",
    )


@allowed_only
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    action = query.data.split(":", 1)[1]
    if action == "today":
        await today_cmd(update, context)
    elif action == "week":
        await week_cmd(update, context)
    elif action == "month":
        await month_cmd(update, context)
    elif action == "balance":
        await balance_cmd(update, context)
    elif action == "export":
        await export_cmd(update, context)
    elif action == "budget":
        if update.callback_query:
            await update.callback_query.answer()
        await _show_budget_overview(update, edit=True)
    elif action == "trends":
        await trends_cmd(update, context)
    elif action == "goals":
        await goals_cmd(update, context)
    elif action == "back":
        if update.callback_query:
            await update.callback_query.answer()
        await start(update, context)


# ── /rate ─────────────────────────────────────────────────────────────────────

@allowed_only
async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user is not None
    args = context.args or []
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        if not args:
            await reply(
                update,
                f"Current KHR rate: <b>1 USD = {user.khr_rate:,} KHR</b>\n"
                "Change it with <code>/rate 4100</code>.",
            )
            return
        try:
            new_rate = int(args[0].replace(",", ""))
        except ValueError:
            await reply(update, "Enter a whole number, e.g. <code>/rate 4100</code>.")
            return
        if new_rate < 100 or new_rate > 1_000_000:
            await reply(update, "Rate must be between 100 and 1,000,000.")
            return
        user.khr_rate = new_rate
    await reply(update, f"KHR rate set: <b>1 USD = {new_rate:,} KHR</b>.")


# ── /edit ─────────────────────────────────────────────────────────────────────

def _tx_label(tx: object) -> str:
    from bot.models import Transaction as Tx
    assert isinstance(tx, Tx)
    sign = "+" if tx.kind == "income" else "-"
    cat = tx.category.name if tx.category else "uncategorized"
    note = f" {tx.note}" if tx.note else ""
    from bot.reports import _fmt_short
    return f"{_fmt_short(tx.occurred_on)}  {sign}{tx.amount_cents / 100:.2f}  {cat}{note}"


def _edit_field_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Amount", callback_data="ef:amount"),
                InlineKeyboardButton("Category", callback_data="ef:category"),
                InlineKeyboardButton("Note", callback_data="ef:note"),
            ],
            [
                InlineKeyboardButton("Delete entry", callback_data="ef:delete"),
                InlineKeyboardButton("✖ Cancel", callback_data="ef:cancel"),
            ],
        ]
    )


@allowed_only
async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.effective_user is not None
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        txs = recent_transactions(session, user.id, 10)
        if not txs:
            await reply(update, "No transactions to edit yet.")
            return ConversationHandler.END
        snapshot = [(tx.id, _tx_label(tx)) for tx in txs]

    rows = [[InlineKeyboardButton(label, callback_data=f"etx:{tx_id}")] for tx_id, label in snapshot]
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data="ef:cancel")])
    await reply(update, "<b>Select a transaction to edit:</b>", InlineKeyboardMarkup(rows))
    return EDIT_TX


@allowed_only
async def edit_pick_tx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    tx_id = int(query.data.split(":", 1)[1])
    context.user_data["edit_tx_id"] = tx_id
    with session_scope() as session:
        assert update.effective_user is not None
        user = get_or_create_user(session, update.effective_user.id)
        tx = get_transaction(session, tx_id, user.id)
        if tx is None:
            await reply(update, "Transaction not found.", edit=True)
            return ConversationHandler.END
        label = _tx_label(tx)
    await reply(
        update,
        f"<b>Editing:</b>\n<code>{escape(label)}</code>\n\nWhat would you like to change?",
        _edit_field_keyboard(),
        edit=True,
    )
    return EDIT_FIELD


@allowed_only
async def edit_pick_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    field = query.data.split(":", 1)[1]

    if field == "cancel":
        await reply(update, "Cancelled.", main_keyboard(), edit=True)
        return ConversationHandler.END

    tx_id = int(context.user_data["edit_tx_id"])

    if field == "delete":
        with session_scope() as session:
            assert update.effective_user is not None
            user = get_or_create_user(session, update.effective_user.id)
            tx = get_transaction(session, tx_id, user.id)
            if tx is None:
                await reply(update, "Transaction not found.", edit=True)
                return ConversationHandler.END
            label = _tx_label(tx)
            session.delete(tx)
        await reply(update, f"Deleted: <code>{escape(label)}</code>", main_keyboard(), edit=True)
        return ConversationHandler.END

    context.user_data["edit_field"] = field

    if field == "amount":
        await reply(update, "Enter the new amount:", edit=True)
        return EDIT_AMOUNT_NEW

    if field == "category":
        with session_scope() as session:
            assert update.effective_user is not None
            user = get_or_create_user(session, update.effective_user.id)
            tx = get_transaction(session, tx_id, user.id)
            if tx is None:
                await reply(update, "Transaction not found.", edit=True)
                return ConversationHandler.END
            choices = [(c.id, c.name) for c in list_categories(session, tx.kind)]
        await reply(update, "Pick a new category:", category_keyboard(choices), edit=True)
        return EDIT_CAT_NEW

    if field == "note":
        await reply(
            update,
            "Enter a new note (or tap Skip to clear it):",
            skip_note_keyboard(),
            edit=True,
        )
        return EDIT_NOTE_NEW

    return ConversationHandler.END


@allowed_only
async def edit_new_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    assert update.effective_user is not None
    tx_id = int(context.user_data["edit_tx_id"])
    parsed = parse_amount_and_currency(update.message.text.strip())
    if parsed is None:
        await reply(update, "Couldn't read that amount. Try like <code>4.50</code>.")
        return EDIT_AMOUNT_NEW
    raw_amount, currency = parsed
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        tx = get_transaction(session, tx_id, user.id)
        if tx is None:
            await reply(update, "Transaction not found.")
            return ConversationHandler.END
        cents = convert_to_cents(raw_amount, currency, user.khr_rate)
        tx.amount_cents = cents
        label = _tx_label(tx)
    await reply(update, f"Updated: <code>{escape(label)}</code>", main_keyboard())
    return ConversationHandler.END


@allowed_only
async def edit_new_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    assert update.effective_user is not None
    tx_id = int(context.user_data["edit_tx_id"])
    category_id = int(query.data.split(":", 1)[1])
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        tx = get_transaction(session, tx_id, user.id)
        if tx is None:
            await reply(update, "Transaction not found.", edit=True)
            return ConversationHandler.END
        tx.category_id = category_id
        label = _tx_label(tx)
    await reply(update, f"Updated: <code>{escape(label)}</code>", main_keyboard(), edit=True)
    return ConversationHandler.END


@allowed_only
async def edit_new_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    assert update.effective_user is not None
    tx_id = int(context.user_data["edit_tx_id"])
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        tx = get_transaction(session, tx_id, user.id)
        if tx is None:
            await reply(update, "Transaction not found.")
            return ConversationHandler.END
        tx.note = update.message.text.strip() or None
        label = _tx_label(tx)
    await reply(update, f"Updated: <code>{escape(label)}</code>", main_keyboard())
    return ConversationHandler.END


@allowed_only
async def edit_note_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    assert update.effective_user is not None
    tx_id = int(context.user_data["edit_tx_id"])
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        tx = get_transaction(session, tx_id, user.id)
        if tx is None:
            await reply(update, "Transaction not found.", edit=True)
            return ConversationHandler.END
        tx.note = None
        label = _tx_label(tx)
    await reply(update, f"Note cleared: <code>{escape(label)}</code>", main_keyboard(), edit=True)
    return ConversationHandler.END


_PERIOD_BUDGET_FIELD = {
    "today": "budget_today_cents",
    "week": "budget_week_cents",
    "month": "budget_month_cents",
}
_PERIOD_LABELS = {"today": "Today", "week": "This Week", "month": "This Month"}


def _budget_bar(pct: int) -> str:
    filled = min(pct, 100) // 10
    return "█" * filled + "░" * (10 - filled)


def _budget_overview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📅 Today", callback_data="bset:today"),
                InlineKeyboardButton("📆 Week", callback_data="bset:week"),
            ],
            [
                InlineKeyboardButton("🗓 Month", callback_data="bset:month"),
                InlineKeyboardButton("◀ Back", callback_data="cmd:back"),
            ],
        ]
    )


@allowed_only
async def budget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_budget_overview(update)


async def _show_budget_overview(update: Update, edit: bool = False) -> None:
    assert update.effective_user is not None
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        lines = ["<b>Budget</b>", "", "Tap a period to set your spending limit.", ""]
        for period, field in _PERIOD_BUDGET_FIELD.items():
            limit = getattr(user, field)
            label = _PERIOD_LABELS[period]
            start, end = period_range(period, user.timezone)
            spent = total_expense_for_period(session, user.id, start, end)
            if limit:
                pct = int(spent / limit * 100)
                bar = _budget_bar(pct)
                over = "  OVER" if spent > limit else ""
                lines.append(
                    f"<b>{label}</b>{over}\n"
                    f"  {format_money(spent, user.currency)} / {format_money(limit, user.currency)}"
                    f"  ({pct}%) {bar}"
                )
            else:
                lines.append(f"<b>{label}</b>  —  not set")

    await reply(update, "\n".join(lines), _budget_overview_keyboard(), edit=edit)


@allowed_only
async def budget_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: user tapped Today / Week / Month to set a budget."""
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    period = query.data.split(":", 1)[1]
    context.user_data["bperiod"] = period

    assert update.effective_user is not None
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        current = getattr(user, _PERIOD_BUDGET_FIELD[period])
        currency = user.currency

    label = _PERIOD_LABELS[period]
    current_str = format_money(current, currency) if current else "not set"
    remove_row: list[list[InlineKeyboardButton]] = (
        [[InlineKeyboardButton("🗑 Remove", callback_data=f"bdel:{period}")]] if current else []
    )
    keyboard = InlineKeyboardMarkup(
        remove_row + [[InlineKeyboardButton("✖ Cancel", callback_data="cmd:budget")]]
    )
    await reply(
        update,
        f"<b>{label} budget</b>\nCurrent: <b>{current_str}</b>\n\n"
        "Enter your total spending limit:",
        keyboard,
        edit=True,
    )
    return BUDGET_SET_INPUT


@allowed_only
async def budget_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    assert update.effective_user is not None
    period = str(context.user_data.pop("bperiod", "month"))
    amount_cents = parse_amount_to_cents(update.message.text.strip())
    if amount_cents is None:
        await reply(update, "Couldn't read that. Try like <code>50</code> or <code>200</code>.")
        return BUDGET_SET_INPUT

    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        setattr(user, _PERIOD_BUDGET_FIELD[period], amount_cents)

    label = _PERIOD_LABELS[period]
    await reply(
        update,
        f"<b>{label} budget</b> set to <b>{format_money(amount_cents)}</b>.",
    )
    await _show_budget_overview(update)
    return ConversationHandler.END


@allowed_only
async def budget_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    period = query.data.split(":", 1)[1]
    context.user_data.pop("bperiod", None)

    assert update.effective_user is not None
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        setattr(user, _PERIOD_BUDGET_FIELD[period], None)

    label = _PERIOD_LABELS[period]
    await reply(update, f"<b>{label} budget</b> removed.", edit=True)
    await _show_budget_overview(update)
    return ConversationHandler.END


@allowed_only
async def budget_period_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles cancel → return to budget overview."""
    query = update.callback_query
    assert query is not None
    await query.answer()
    await _show_budget_overview(update, edit=True)


# ── /trends ───────────────────────────────────────────────────────────────────

@allowed_only
async def trends_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    assert update.effective_user is not None

    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        from datetime import timedelta
        from bot.db import income_expense_for_period as _iep
        today = local_today(user.timezone)
        week_start = today - timedelta(days=today.weekday())
        last_week_end = week_start - timedelta(days=1)
        last_week_start = last_week_end - timedelta(days=6)
        month_start = today.replace(day=1)
        last_month_end = month_start - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        w_inc, w_exp = _iep(session, user.id, week_start, today)
        lw_inc, lw_exp = _iep(session, user.id, last_week_start, last_week_end)
        m_inc, m_exp = _iep(session, user.id, month_start, today)
        lm_inc, lm_exp = _iep(session, user.id, last_month_start, last_month_end)

        text = build_trends_report(session, user.id, user.timezone, user.currency)
        currency = user.currency

    chart_buf = build_trends_chart(
        w_inc=w_inc, w_exp=w_exp,
        lw_inc=lw_inc, lw_exp=lw_exp,
        m_inc=m_inc, m_exp=m_exp,
        lm_inc=lm_inc, lm_exp=lm_exp,
        currency=currency,
    )

    msg = update.effective_message
    if msg:
        await msg.reply_photo(photo=chart_buf, caption="📊 Trends", parse_mode=ParseMode.HTML)
    await reply(update, text, main_keyboard())


# ── /report (custom date range) ───────────────────────────────────────────────

@allowed_only
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.effective_user is not None
    args = context.args or []
    if len(args) >= 2:
        today = local_today("Asia/Phnom_Penh")
        start = parse_date_input(args[0], today.year)
        end = parse_date_input(args[1], today.year)
        if start and end:
            with session_scope() as session:
                user = get_or_create_user(session, update.effective_user.id)
                text = build_custom_range_report(
                    session, user.id, start, end, user.timezone, user.currency
                )
            await reply(update, text, main_keyboard())
            return ConversationHandler.END
    await reply(
        update,
        "Enter the <b>start date</b>:\n<code>DD/MM/YYYY</code>  or  <code>YYYY-MM-DD</code>",
        cancel_keyboard(),
    )
    return REPORT_START


@allowed_only
async def report_start_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    assert update.effective_user is not None
    today = local_today("Asia/Phnom_Penh")
    start = parse_date_input(update.message.text.strip(), today.year)
    if start is None:
        await reply(update, "Couldn't read that date. Try <code>01/08/2026</code>.")
        return REPORT_START
    context.user_data["report_start"] = start.isoformat()
    await reply(
        update,
        f"Start: <b>{start.strftime('%d %b %Y')}</b>\n\nNow enter the <b>end date</b>:",
        cancel_keyboard(),
    )
    return REPORT_END


@allowed_only
async def report_end_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    assert update.effective_user is not None
    from datetime import date as date_cls
    today = local_today("Asia/Phnom_Penh")
    end = parse_date_input(update.message.text.strip(), today.year)
    if end is None:
        await reply(update, "Couldn't read that date. Try <code>14/08/2026</code>.")
        return REPORT_END
    start = date_cls.fromisoformat(str(context.user_data.pop("report_start", today.isoformat())))
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        text = build_custom_range_report(
            session, user.id, start, end, user.timezone, user.currency
        )
    await reply(update, text, main_keyboard())
    return ConversationHandler.END


# ── /goal ─────────────────────────────────────────────────────────────────────

def _goals_keyboard(goals: list) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for g in goals:
        rows.append([
            InlineKeyboardButton(f"💰 Deposit  {g.name}", callback_data=f"gdep:{g.id}"),
            InlineKeyboardButton("🗑", callback_data=f"grem:{g.id}"),
        ])
    rows.append([InlineKeyboardButton("➕ Add goal", callback_data="gadd")])
    rows.append([InlineKeyboardButton("◀ Back", callback_data="cmd:back")])
    return InlineKeyboardMarkup(rows)


@allowed_only
async def goals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    await _show_goals(update)


async def _show_goals(update: Update, edit: bool = False) -> None:
    assert update.effective_user is not None
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        goals = list_goals(session, user.id)
        currency = user.currency

    lines = ["<b>Savings Goals</b>", ""]
    if not goals:
        lines.append("No goals yet. Tap <b>Add goal</b> to create one.")
    else:
        for g in goals:
            pct = int(g.current_cents / g.target_cents * 100) if g.target_cents else 0
            pct = min(pct, 100)
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            achieved = "  ✅ Achieved!" if g.current_cents >= g.target_cents else ""
            lines.append(
                f"<b>{escape(g.name)}</b>{achieved}\n"
                f"  {format_money(g.current_cents, currency)} / {format_money(g.target_cents, currency)}"
                f"  ({pct}%) {bar}"
            )

    if goals and not edit:
        chart_buf = build_goal_chart(
            goals=[
                {"name": g.name, "current_cents": g.current_cents, "target_cents": g.target_cents}
                for g in goals
            ],
            currency=currency,
        )
        msg = update.effective_message
        if msg:
            await msg.reply_photo(photo=chart_buf)

    await reply(update, "\n".join(lines), _goals_keyboard(goals), edit=edit)


@allowed_only
async def goal_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    await reply(
        update,
        "Enter a name for your goal (e.g. <code>Vacation</code>):",
        cancel_keyboard(),
        edit=True,
    )
    return GOAL_NAME


@allowed_only
async def goal_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    name = update.message.text.strip()
    if not name or len(name) > 64:
        await reply(update, "Please enter a short name (max 64 characters).")
        return GOAL_NAME
    context.user_data["goal_name"] = name
    await reply(
        update,
        f"Goal: <b>{escape(name)}</b>\n\nEnter the target amount (e.g. <code>500</code>):",
        cancel_keyboard(),
    )
    return GOAL_AMOUNT


@allowed_only
async def goal_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    assert update.effective_user is not None
    amount_cents = parse_amount_to_cents(update.message.text.strip())
    if amount_cents is None:
        await reply(update, "Couldn't read that amount. Try like <code>500</code>.")
        return GOAL_AMOUNT
    name = str(context.user_data.pop("goal_name", "Goal"))
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        add_goal(session, user.id, name, amount_cents)
    await reply(
        update,
        f"Goal <b>{escape(name)}</b> set to <b>{format_money(amount_cents)}</b>.",
    )
    await _show_goals(update)
    return ConversationHandler.END


@allowed_only
async def goal_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    assert update.effective_user is not None
    goal_id = int(query.data.split(":", 1)[1])
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        delete_goal(session, goal_id, user.id)
    await _show_goals(update, edit=True)


@allowed_only
async def goal_deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None and query.data is not None
    await query.answer()
    goal_id = int(query.data.split(":", 1)[1])
    context.user_data["deposit_goal_id"] = goal_id
    assert update.effective_user is not None
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        goals = list_goals(session, user.id)
    goal = next((g for g in goals if g.id == goal_id), None)
    name = escape(goal.name) if goal else "goal"
    await reply(
        update,
        f"How much do you want to add to <b>{name}</b>?\n<i>E.g. 50, 1.5k, ₭20000</i>",
        cancel_keyboard(),
        edit=True,
    )
    return GOAL_DEPOSIT


@allowed_only
async def goal_deposit_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None and update.message.text is not None
    assert update.effective_user is not None
    amount_cents = parse_amount_to_cents(update.message.text.strip())
    if amount_cents is None or amount_cents <= 0:
        await reply(update, "Couldn't read that amount. Try like <code>50</code>.")
        return GOAL_DEPOSIT
    goal_id = int(context.user_data.pop("deposit_goal_id", 0))
    with session_scope() as session:
        user = get_or_create_user(session, update.effective_user.id)
        goal = deposit_to_goal(session, goal_id, user.id, amount_cents)
        if goal is None:
            await reply(update, "Goal not found.")
            return ConversationHandler.END
        name = goal.name
        current = goal.current_cents
        target = goal.target_cents
        currency = user.currency
    pct = min(int(current / target * 100) if target else 0, 100)
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    achieved = "\n\nYou reached your goal!" if current >= target else ""
    await reply(
        update,
        f"Added <b>{format_money(amount_cents, currency)}</b> to <b>{escape(name)}</b>.\n\n"
        f"{format_money(current, currency)} / {format_money(target, currency)}"
        f"  ({pct}%) {bar}{achieved}",
    )
    await _show_goals(update)
    return ConversationHandler.END


def register_handlers(application: Application) -> None:
    edit_conversation = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_cmd)],
        states={
            EDIT_TX: [CallbackQueryHandler(edit_pick_tx, pattern=r"^etx:\d+$")],
            EDIT_FIELD: [CallbackQueryHandler(edit_pick_field, pattern=r"^ef:(amount|category|note|delete|cancel)$")],
            EDIT_AMOUNT_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new_amount)],
            EDIT_CAT_NEW: [CallbackQueryHandler(edit_new_category, pattern=r"^cat:\d+$")],
            EDIT_NOTE_NEW: [
                CallbackQueryHandler(edit_note_skip, pattern=r"^note:skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new_note),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^log:cancel$"),
            CallbackQueryHandler(edit_pick_field, pattern=r"^ef:cancel$"),
        ],
        allow_reentry=True,
        name="edit_conversation",
        persistent=False,
    )
    application.add_handler(edit_conversation)

    log_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("expense", expense_cmd),
            CommandHandler("income", income_cmd),
            CallbackQueryHandler(start_log_callback, pattern=r"^log:(income|expense)$"),
        ],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received)],
            CATEGORY: [
                CallbackQueryHandler(category_callback, pattern=r"^cat:\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, category_text),
            ],
            NOTE: [
                CallbackQueryHandler(note_skip, pattern=r"^note:skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, note_received),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^log:cancel$"),
            CommandHandler("start", start),
            CommandHandler("help", help_cmd),
        ],
        allow_reentry=True,
        name="log_conversation",
        persistent=False,
    )
    application.add_handler(log_conversation)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CommandHandler("week", week_cmd))
    application.add_handler(CommandHandler("month", month_cmd))
    application.add_handler(CommandHandler("balance", balance_cmd))
    application.add_handler(CommandHandler("undo", undo_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("categories", categories_cmd))
    application.add_handler(CommandHandler("remind", remind_cmd))
    budget_set_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(budget_set_start, pattern=r"^bset:(today|week|month)$")],
        states={
            BUDGET_SET_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, budget_amount_received)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(budget_period_callback, pattern=r"^cmd:budget$"),
            CallbackQueryHandler(budget_remove_callback, pattern=r"^bdel:(today|week|month)$"),
        ],
        allow_reentry=True,
        name="budget_set_conversation",
        persistent=False,
    )
    application.add_handler(budget_set_conversation)

    application.add_handler(CommandHandler("budget", budget_cmd))
    application.add_handler(CommandHandler("rate", rate_cmd))
    application.add_handler(CommandHandler("edit", edit_cmd))
    application.add_handler(CommandHandler("backup", backup_cmd))
    report_conversation = ConversationHandler(
        entry_points=[CommandHandler("report", report_cmd)],
        states={
            REPORT_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_start_received)],
            REPORT_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_end_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern=r"^log:cancel$")],
        allow_reentry=True,
        name="report_conversation",
        persistent=False,
    )
    application.add_handler(report_conversation)

    goal_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(goal_add_start, pattern=r"^gadd$"),
            CallbackQueryHandler(goal_deposit_start, pattern=r"^gdep:\d+$"),
        ],
        states={
            GOAL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, goal_name_received)],
            GOAL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, goal_amount_received)],
            GOAL_DEPOSIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, goal_deposit_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern=r"^log:cancel$")],
        allow_reentry=True,
        name="goal_conversation",
        persistent=False,
    )
    application.add_handler(goal_conversation)

    application.add_handler(CommandHandler("trends", trends_cmd))
    application.add_handler(CommandHandler("goals", goals_cmd))
    application.add_handler(CallbackQueryHandler(goal_remove_callback, pattern=r"^grem:\d+$"))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^cmd:(today|week|month|balance|export|budget|trends|goals|back)$"))
    application.add_handler(CallbackQueryHandler(delete_category_callback, pattern=r"^delcat:\d+$"))
