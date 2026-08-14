from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from bot.models import Transaction
from bot.money import format_money


def local_today(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name)).date()


def period_range(period: str, timezone_name: str) -> tuple[date, date]:
    today = local_today(timezone_name)
    if period == "today":
        return today, today
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start, today
    if period == "month":
        return today.replace(day=1), today
    raise ValueError(f"Unknown period: {period}")


def period_title(period: str, start: date, end: date) -> str:
    if period == "today":
        return f"Today ({_fmt_date(end)})"
    if period == "week":
        return f"This week ({_fmt_date(start)} – {_fmt_date(end)})"
    return f"This month ({end.strftime('%B %Y')})"


def _fmt_date(value: date) -> str:
    return value.strftime("%d %b %Y")


def fetch_transactions(
    session: Session, user_id: int, start: date, end: date
) -> list[Transaction]:
    return list(
        session.scalars(
            select(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.occurred_on >= start,
                Transaction.occurred_on <= end,
            )
            .options(joinedload(Transaction.category))
            .order_by(Transaction.occurred_on.asc(), Transaction.created_at.asc())
        ).unique()
    )


def sum_by_kind(session: Session, user_id: int) -> tuple[int, int]:
    rows = session.execute(
        select(Transaction.kind, func.coalesce(func.sum(Transaction.amount_cents), 0))
        .where(Transaction.user_id == user_id)
        .group_by(Transaction.kind)
    ).all()
    income = 0
    expense = 0
    for kind, total in rows:
        if kind == "income":
            income = int(total)
        elif kind == "expense":
            expense = int(total)
    return income, expense


def count_on_date(session: Session, user_id: int, day: date) -> int:
    return int(
        session.scalar(
            select(func.count()).where(
                Transaction.user_id == user_id,
                Transaction.occurred_on == day,
            )
        )
        or 0
    )


def build_period_report(
    session: Session,
    user_id: int,
    period: str,
    timezone_name: str,
    currency: str,
) -> str:
    start, end = period_range(period, timezone_name)
    txs = fetch_transactions(session, user_id, start, end)
    income = sum(tx.amount_cents for tx in txs if tx.kind == "income")
    expense = sum(tx.amount_cents for tx in txs if tx.kind == "expense")
    net = income - expense

    lines = [
        f"<b>{escape(period_title(period, start, end))}</b>",
        "",
        f"Income: {format_money(income, currency)}",
        f"Expense: {format_money(expense, currency)}",
        f"Net: {format_money(net, currency)}",
    ]

    if not txs:
        lines += ["", "No transactions yet."]
        return "\n".join(lines)

    by_cat: dict[tuple[str, str], int] = defaultdict(int)
    for tx in txs:
        name = tx.category.name if tx.category else "uncategorized"
        by_cat[(tx.kind, name)] += tx.amount_cents

    lines += ["", "<b>By category</b>"]
    for kind in ("income", "expense"):
        items = sorted(
            ((name, total) for (k, name), total in by_cat.items() if k == kind),
            key=lambda item: (-item[1], item[0]),
        )
        if not items:
            continue
        lines.append(f"{kind.title()}")
        for name, total in items:
            lines.append(f"  {escape(name)}: {format_money(total, currency)}")

    lines += ["", "<b>Entries</b>"]
    for tx in txs[:40]:
        sign = "+" if tx.kind == "income" else "-"
        cat = tx.category.name if tx.category else "uncategorized"
        note = f" — {escape(tx.note)}" if tx.note else ""
        lines.append(
            f"{_fmt_date(tx.occurred_on)} {sign}{format_money(tx.amount_cents, currency)} "
            f"{escape(cat)}{note}"
        )
    if len(txs) > 40:
        lines.append(f"…and {len(txs) - 40} more. Use /export for the full list.")
    return "\n".join(lines)


def build_balance_report(
    session: Session, user_id: int, currency: str
) -> str:
    income, expense = sum_by_kind(session, user_id)
    net = income - expense
    return "\n".join(
        [
            "<b>All-time balance</b>",
            "",
            f"Income: {format_money(income, currency)}",
            f"Expense: {format_money(expense, currency)}",
            f"Balance: {format_money(net, currency)}",
        ]
    )


def build_csv(session: Session, user_id: int) -> bytes:
    txs = list(
        session.scalars(
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .options(joinedload(Transaction.category))
            .order_by(Transaction.occurred_on.asc(), Transaction.created_at.asc())
        ).unique()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "kind", "amount", "category", "note", "created_at"])
    for tx in txs:
        amount = f"{tx.amount_cents / 100:.2f}"
        category = tx.category.name if tx.category else ""
        created = tx.created_at.isoformat() if tx.created_at else ""
        writer.writerow(
            [tx.occurred_on.isoformat(), tx.kind, amount, category, tx.note or "", created]
        )
    return buf.getvalue().encode("utf-8-sig")
