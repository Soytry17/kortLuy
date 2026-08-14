from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_AMOUNT = Decimal("100000000")


def parse_amount_to_cents(text: str) -> int | None:
    cleaned = (
        text.strip()
        .replace(",", "")
        .replace("$", "")
        .replace("€", "")
        .replace("£", "")
    )
    if cleaned.lower().endswith("usd"):
        cleaned = cleaned[:-3].strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value <= 0 or value > MAX_AMOUNT:
        return None
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def format_money(cents: int, currency: str = "USD") -> str:
    negative = cents < 0
    cents = abs(cents)
    major, minor = divmod(cents, 100)
    amount = f"{major:,}.{minor:02d}"
    if negative:
        amount = f"-{amount}"
    if currency.upper() == "USD":
        if amount.startswith("-"):
            return f"-${amount[1:]}"
        return f"${amount}"
    return f"{amount} {currency}"
