from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_AMOUNT = Decimal("100000000")


def parse_amount_and_currency(text: str) -> tuple[Decimal, str] | None:
    """Return (amount, currency_code) or None.

    Recognised currency markers:
    - KHR: trailing 'khr' (case-insensitive) or leading '₭'
    - USD: default; '$' prefix, trailing 'usd', or no marker
    - 'k' suffix is a thousands multiplier for any currency (1.5k → 1500)
    """
    raw = text.strip()
    currency = "USD"

    lower = raw.lower()
    if lower.endswith("khr"):
        raw = raw[:-3].strip()
        currency = "KHR"
    elif raw.startswith("₭"):
        raw = raw[1:].strip()
        currency = "KHR"

    cleaned = (
        raw.replace(",", "")
        .replace("$", "")
        .replace("€", "")
        .replace("£", "")
    )
    if cleaned.lower().endswith("usd"):
        cleaned = cleaned[:-3].strip()

    multiplier = Decimal("1")
    if cleaned.lower().endswith("k"):
        cleaned = cleaned[:-1]
        multiplier = Decimal("1000")

    if not cleaned:
        return None
    try:
        value = Decimal(cleaned) * multiplier
    except InvalidOperation:
        return None
    if value <= 0 or value > MAX_AMOUNT:
        return None
    return value, currency


def parse_amount_to_cents(text: str) -> int | None:
    """Parse a USD amount string to integer cents."""
    result = parse_amount_and_currency(text)
    if result is None:
        return None
    value, currency = result
    if currency == "KHR":
        # When called without a KHR rate, just parse as-is (used by /budget)
        value = value
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def convert_to_cents(amount: Decimal, currency: str, khr_rate: int) -> int:
    """Convert an amount in any supported currency to USD cents."""
    if currency == "KHR":
        usd = amount / Decimal(str(khr_rate))
        return int(usd.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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


def format_khr(amount: Decimal) -> str:
    """Format a KHR amount for display (no decimal places)."""
    return f"₭{int(amount):,}"
