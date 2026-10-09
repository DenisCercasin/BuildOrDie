import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")


def today() -> date:
    return datetime.now(SGT).date()


def now() -> datetime:
    return datetime.now(SGT)


def money(minor: int) -> str:
    sign = "−" if minor < 0 else ""
    return f"{sign}S${abs(minor) // 100}.{abs(minor) % 100:02d}"


def parse_amount_minor(text: str) -> int:
    if re.search(r"\d+\.\d{3,}", text):
        raise ValueError("Use at most two decimal places for SGD amounts.")
    match = re.search(r"(?:S\$|\$|SGD\s*)?\s*(\d+(?:\.\d{1,2})?)", text, re.I)
    if not match:
        raise ValueError("Enter an amount such as S$35 or S$35.50.")
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError("Enter a valid SGD amount.") from exc
    if amount <= 0:
        raise ValueError("The amount must be greater than zero.")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_deadline(text: str, base: date | None = None) -> date:
    base = base or today()
    lower = text.lower().strip()
    days = re.search(r"\b(\d{1,3})\s*days?\b", lower)
    if days:
        result = base + timedelta(days=int(days.group(1)))
    elif re.search(r"\b(two weeks|2 weeks|a fortnight)\b", lower):
        result = base + timedelta(days=14)
    elif re.search(r"\b(one week|a week)\b", lower):
        result = base + timedelta(days=7)
    elif match := re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", lower):
        try:
            result = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError as exc:
            raise ValueError("That date is invalid. Use YYYY-MM-DD.") from exc
    elif match := re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:,?\s+(20\d{2}))?\b",
        lower,
    ):
        months = [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ]
        year = int(match.group(3)) if match.group(3) else base.year
        try:
            result = date(year, months.index(match.group(1)) + 1, int(match.group(2)))
            if not match.group(3) and result < base:
                result = result.replace(year=year + 1)
        except ValueError as exc:
            raise ValueError("That date is invalid. Use YYYY-MM-DD.") from exc
    else:
        raise ValueError("Tell me a number of days or a date like 2026-10-25.")
    if result < base:
        raise ValueError("That date has passed. Please choose a future pickup date.")
    return result
