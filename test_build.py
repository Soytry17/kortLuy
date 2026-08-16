"""Pre-push build test — run with: python test_build.py"""
from __future__ import annotations

import sys

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}" + (f": {detail}" if detail else ""))
        failures.append(label)


# ── Imports ──────────────────────────────────────────────────────────────────
print("=== Imports ===")
try:
    from bot.config import get_settings
    from bot.db import init_db, income_expense_for_period
    from bot.models import User, Transaction, Category, CategoryBudget, SavingsGoal
    from bot.money import parse_amount_to_cents, format_money, parse_amount_and_currency
    from bot.reports import build_custom_range_report, sum_by_kind, parse_date_input
    from bot.charts import build_goal_chart
    from bot.handlers import main_keyboard, safe_answer
    print("  OK  all modules")
except Exception as e:
    print(f"  FAIL import: {e}")
    failures.append("imports")

# ── Money ────────────────────────────────────────────────────────────────────
print("\n=== Money ===")
check("parse 10 -> 1000 cents",      parse_amount_to_cents("10") == 1000,      str(parse_amount_to_cents("10")))
check("parse 1.5k -> 150000 cents",  parse_amount_to_cents("1.5k") == 150000,  str(parse_amount_to_cents("1.5k")))
check("parse 0.50 -> 50 cents",      parse_amount_to_cents("0.50") == 50,      str(parse_amount_to_cents("0.50")))
check("format 1000 cents -> $10.00", format_money(1000) == "$10.00",           format_money(1000))
check("format 50 cents -> $0.50",    format_money(50) == "$0.50",              format_money(50))
check("parse bad -> None",           parse_amount_to_cents("abc") is None)

# ── Date parsing ─────────────────────────────────────────────────────────────
print("\n=== Date parsing ===")
from datetime import date
check("DD/MM/YYYY",  parse_date_input("01/08/2026", 2026) == date(2026, 8, 1))
check("YYYY-MM-DD",  parse_date_input("2026-08-15", 2026) == date(2026, 8, 15))
check("DD/MM",       parse_date_input("15/08", 2026) == date(2026, 8, 15))
check("bad -> None", parse_date_input("not-a-date", 2026) is None)

# ── Chart ────────────────────────────────────────────────────────────────────
print("\n=== Charts ===")
try:
    buf = build_goal_chart(goals=[
        {"name": "Vacation",  "current_cents": 32000, "target_cents": 50000},
        {"name": "Emergency", "current_cents":  5000, "target_cents": 100000},
    ])
    size = len(buf.read())
    check(f"goals chart PNG ({size:,} bytes)", size > 5000, f"{size} bytes")
except Exception as e:
    check("goals chart", False, str(e))

# ── DB ───────────────────────────────────────────────────────────────────────
print("\n=== Database ===")
try:
    init_db()
    check("init_db", True)
except Exception as e:
    check("init_db", False, str(e))

# ── Result ───────────────────────────────────────────────────────────────────
print()
if failures:
    print(f"FAILED — {len(failures)} test(s): {', '.join(failures)}")
    sys.exit(1)
else:
    print("ALL TESTS PASSED — safe to push")
