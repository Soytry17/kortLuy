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

from bot.config import get_settings
from bot.db import (
    add_category,
    add_transaction,
    category_usage_count,
    find_category,
    get_or_create_user,
    last_transaction,
    list_categories,
    session_scope,
)
from bot.models import Category, User
from bot.money import format_money, parse_amount_to_cents
from bot.reminders import parse_hhmm, schedule_user_reminder
from bot.reports import (
    build_balance_report,
    build_csv,
    build_period_report,
    local_today,
)

AMOUNT, CATEGORY, NOTE = range(3)
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
                InlineKeyboardButton("Expense", callback_data="log:expense"),
                InlineKeyboardButton("Income", callback_data="log:income"),
            ],
            [
                InlineKeyboardButton("Today", callback_data="cmd:today"),
                InlineKeyboardButton("Week", callback_data="cmd:week"),
                InlineKeyboardButton("Month", callback_data="cmd:month"),
            ],
            [
                InlineKeyboardButton("Balance", callback_data="cmd:balance"),
                InlineKeyboardButton("Export", callback_data="cmd:export"),
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
    rows.append([InlineKeyboardButton("Cancel", callback_data="log:cancel")])
    return InlineKeyboardMarkup(rows)


def skip_note_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Skip", callback_data="note:skip"),
                InlineKeyboardButton("Cancel", callback_data="log:cancel"),
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


HELP_TEXT = (
    "<b>Money tracker</b>\n\n"
    "Log a full line, or tap a button and follow the prompts.\n\n"
    "<code>/expense 4.50 food lunch</code>\n"
    "<code>/income 500 salary payday</code>\n\n"
    "<b>Reports</b>\n"
    "/today  /week  /month  /balance\n\n"
    "<b>Other</b>\n"
    "/categories — list, add, delete\n"
    "/undo — remove the last entry\n"
    "/export — CSV of all rows\n"
    "/remind 21:00 — daily ping\n"
    "/cancel — stop a prompt"
)


@allowed_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.effective_user is not None
    _maybe_schedule(context, update.effective_user.id)
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
    await reply(update, prompt, edit=True)
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
        await reply(update, prompt)
        return AMOUNT
    return await _apply_amount_and_rest(update, context, arg_text)


async def _apply_amount_and_rest(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> int:
    kind = str(context.user_data.get("kind"))
    parts = text.split()
    cents = parse_amount_to_cents(parts[0])
    if cents is None:
        await reply(
            update,
            "I couldn't read that amount. Try like <code>4.50</code>.",
        )
        return AMOUNT
    context.user_data["amount_cents"] = cents
    rest = parts[1:]
    with session_scope() as session:
        if rest:
            category = find_category(session, rest[0], kind)
            if category:
                note = " ".join(rest[1:]) or None
                return await _save_transaction(
                    update, context, session, category.id, note
                )
            context.user_data["pending_note"] = " ".join(rest)
        choices = [(c.id, c.name) for c in list_categories(session, kind)]
    hint = ""
    if context.user_data.get("pending_note"):
        hint = (
            f"\nUnknown category <code>{escape(str(context.user_data['pending_note']).split()[0])}</code>. "
            "Pick one below — leftover text will be saved as the note."
        )
    await reply(
        update,
        f"Amount: <b>{format_money(cents)}</b>\nPick a category.{hint}",
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
    msg = (
        f"Logged {kind}: <b>{sign}{format_money(cents, user.currency)}</b>\n"
        f"Category: {escape(cat_name)}{note_bit}"
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


def register_handlers(application: Application) -> None:
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
        ],
        allow_reentry=True,
        name="log_conversation",
        persistent=False,
    )
    application.add_handler(log_conversation)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CommandHandler("week", week_cmd))
    application.add_handler(CommandHandler("month", month_cmd))
    application.add_handler(CommandHandler("balance", balance_cmd))
    application.add_handler(CommandHandler("undo", undo_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("categories", categories_cmd))
    application.add_handler(CommandHandler("remind", remind_cmd))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^cmd:(today|week|month|balance|export)$"))
    application.add_handler(CallbackQueryHandler(delete_category_callback, pattern=r"^delcat:\d+$"))
