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
        return f"Today  •  {_fmt_date(end)}"
    if period == "week":
        if start.month == end.month:
            return f"This week  •  {start.day}–{_fmt_date(end)}"
        return f"This week  •  {_fmt_date(start)} – {_fmt_date(end)}"
    return f"This month  •  {end.strftime('%B %Y')}"


def _fmt_date(value: date) -> str:
    return value.strftime("%d %b %Y")


def _fmt_short(value: date) -> str:
    return value.strftime("%d %b")


def _bar(pct: int, width: int = 8) -> str:
    filled = min(pct, 100) * width // 100
    return "█" * filled + "░" * (width - filled)


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
    net_sign = "+" if net >= 0 else ""
    net_emoji = "📈" if net >= 0 else "📉"

    lines = [
        f"<b>{escape(period_title(period, start, end))}</b>",
        f"<code>{'─' * 30}</code>",
        f"  💰 Income    <b>{format_money(income, currency)}</b>",
        f"  💸 Expense   <b>{format_money(expense, currency)}</b>",
        f"  {net_emoji} Net       <b>{net_sign}{format_money(net, currency)}</b>",
        f"<code>{'─' * 30}</code>",
    ]

    if not txs:
        lines += ["", "<i>No transactions yet.</i>"]
        return "\n".join(lines)

    # ── By category ────────────────────────────────────────────────────────
    by_cat: dict[tuple[str, str], int] = defaultdict(int)
    for tx in txs:
        name = tx.category.name if tx.category else "uncategorized"
        by_cat[(tx.kind, name)] += tx.amount_cents

    lines += ["", "<b>Breakdown</b>"]
    for kind, kind_total in (("income", income), ("expense", expense)):
        items = sorted(
            ((name, total) for (k, name), total in by_cat.items() if k == kind),
            key=lambda item: (-item[1], item[0]),
        )
        if not items:
            continue
        icon = "💰" if kind == "income" else "💸"
        lines.append(f"{icon} <i>{kind.title()}</i>")
        for name, total in items:
            pct = int(total / kind_total * 100) if kind_total else 0
            bar = _bar(pct)
            lines.append(
                f"  <code>{escape(name):<14}</code>"
                f" <b>{format_money(total, currency):>9}</b>"
                f"  {bar} {pct}%"
            )

    # ── Entries ────────────────────────────────────────────────────────────
    count = len(txs)
    lines += ["", f"<b>Entries</b>  <i>({count})</i>"]
    for tx in txs[:40]:
        icon = "➕" if tx.kind == "income" else "➖"
        cat = tx.category.name if tx.category else "uncategorized"
        note = f"  <i>{escape(tx.note)}</i>" if tx.note else ""
        lines.append(
            f"  {icon} <code>{_fmt_short(tx.occurred_on)}</code>"
            f"  <b>{format_money(tx.amount_cents, currency)}</b>"
            f"  <i>{escape(cat)}</i>{note}"
        )
    if count > 40:
        lines.append(f"  <i>…and {count - 40} more. Use /export for the full list.</i>")

    return "\n".join(lines)


def build_balance_report(
    session: Session, user_id: int, currency: str
) -> str:
    income, expense = sum_by_kind(session, user_id)
    net = income - expense
    net_sign = "+" if net >= 0 else ""
    return "\n".join(
        [
            "<b>All-time balance</b>",
            "",
            f"  Income    <b>{format_money(income, currency)}</b>",
            f"  Expense   <b>{format_money(expense, currency)}</b>",
            f"  Balance   <b>{net_sign}{format_money(net, currency)}</b>",
        ]
    )



def parse_date_input(text: str, current_year: int) -> date | None:
    """Parse DD/MM/YYYY, DD/MM, or YYYY-MM-DD."""
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d/%m", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            d = datetime.strptime(text, fmt).date()
            if fmt == "%d/%m":
                d = d.replace(year=current_year)
            return d
        except ValueError:
            continue
    return None


def build_custom_range_report(
    session: Session,
    user_id: int,
    start: date,
    end: date,
    timezone_name: str,
    currency: str,
) -> str:
    if start > end:
        start, end = end, start
    txs = fetch_transactions(session, user_id, start, end)
    income = sum(tx.amount_cents for tx in txs if tx.kind == "income")
    expense = sum(tx.amount_cents for tx in txs if tx.kind == "expense")
    net = income - expense
    net_sign = "+" if net >= 0 else ""

    lines = [
        f"<b>{_fmt_date(start)} – {_fmt_date(end)}</b>",
        "",
        f"  Income    <b>{format_money(income, currency)}</b>",
        f"  Expense   <b>{format_money(expense, currency)}</b>",
        f"  Net       <b>{net_sign}{format_money(net, currency)}</b>",
    ]

    if not txs:
        lines += ["", "<i>No transactions in this range.</i>"]
        return "\n".join(lines)

    from collections import defaultdict
    by_cat: dict[tuple[str, str], int] = defaultdict(int)
    for tx in txs:
        name = tx.category.name if tx.category else "uncategorized"
        by_cat[(tx.kind, name)] += tx.amount_cents

    lines += ["", "<b>Breakdown</b>"]
    for kind, total in (("income", income), ("expense", expense)):
        items = sorted(
            ((n, t) for (k, n), t in by_cat.items() if k == kind),
            key=lambda x: -x[1],
        )
        if items:
            lines.append(f"<i>{kind.title()}</i>")
            for name, amt in items:
                pct = int(amt / total * 100) if total else 0
                lines.append(f"  {escape(name):<14}  {format_money(amt, currency):>10}  {pct}%")

    lines += ["", f"<b>Entries</b>  <i>({len(txs)})</i>"]
    for tx in txs[:40]:
        sign = "+" if tx.kind == "income" else "-"
        cat = tx.category.name if tx.category else "uncategorized"
        note = f"  <i>{escape(tx.note)}</i>" if tx.note else ""
        lines.append(
            f"  <code>{_fmt_short(tx.occurred_on)}</code>"
            f"  {sign}{format_money(tx.amount_cents, currency)}"
            f"  {escape(cat)}{note}"
        )
    if len(txs) > 40:
        lines.append(f"  <i>…and {len(txs) - 40} more. Use /export for the full list.</i>")

    return "\n".join(lines)


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
