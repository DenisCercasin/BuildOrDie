"""SIMULATED merchant data for development and the demo.

None of these prices, fees or thresholds are real store terms. Person 2 will
replace this with verified data in the same shape (MerchantPolicy/MerchantOffer).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal as D

from .models import BookRequest, MerchantOffer, MerchantPolicy, VolumeTier

KINO = "kinokuniya_sg"
POPULAR = "popular_sg"

PICKUP_NTU = "NTU-NorthSpine"
PICKUP_NUS = "NUS-UTown"

BOOKS = {
    "9781847941831": "Atomic Habits",
    "9780349411903": "Deep Work",
    "9780857197689": "The Psychology of Money",
    "9780141983479": "Thinking, Fast and Slow",
    "9780062316097": "Sapiens",
}

MERCHANTS = [
    MerchantPolicy(
        merchant_id=KINO,
        name="Kinokuniya (simulated)",
        shipping_fee=D("5.00"),
        free_shipping_threshold=D("80.00"),
        volume_discounts=[VolumeTier(min_items=5, percent_off=D("5"))],
        is_simulated=True,
    ),
    MerchantPolicy(
        merchant_id=POPULAR,
        name="POPULAR (simulated)",
        shipping_fee=D("4.99"),
        free_shipping_threshold=D("60.00"),
        is_simulated=True,
    ),
]

_PRICES = {
    # book_id: {merchant: (price, delivery_days, stock_qty)}
    "9781847941831": {KINO: ("27.90", 3, None), POPULAR: ("26.00", 4, 20)},
    "9780349411903": {KINO: ("25.00", 3, None), POPULAR: ("24.50", 4, 15)},
    "9780857197689": {KINO: ("23.00", 3, None), POPULAR: ("22.90", 4, 10)},
    "9780141983479": {KINO: ("24.95", 3, None), POPULAR: ("26.50", 5, 6)},
    "9780062316097": {KINO: ("29.90", 3, None), POPULAR: ("28.00", 4, 3)},
}


def offers(fetched_at: datetime | None = None, overrides: dict | None = None) -> list[MerchantOffer]:
    """All simulated offers. ``overrides[(merchant, book)] = {field: value}`` tweaks one."""
    out = []
    for book_id, by_m in _PRICES.items():
        for mid, (price, days, stock) in by_m.items():
            fields = dict(
                merchant_id=mid,
                book_id=book_id,
                price=D(price),
                delivery_days=days,
                stock_qty=stock,
                fetched_at=fetched_at,
                is_simulated=True,
            )
            if overrides and (mid, book_id) in overrides:
                fields.update(overrides[(mid, book_id)])
            out.append(MerchantOffer(**fields))
    return out


def request(rid: str, user: str, book_id: str, today: date, days: int, **kw) -> BookRequest:
    return BookRequest(
        request_id=rid,
        user_id=user,
        book_id=book_id,
        title=BOOKS.get(book_id),
        deadline=today + timedelta(days=days),
        pickup_point=kw.pop("pickup_point", PICKUP_NTU),
        **kw,
    )


def demo_requests(today: date) -> dict[str, BookRequest]:
    """The Alice / Bob / Charlie scenario from the project brief."""
    return {
        "alice": request("req_alice", "tg_alice", "9781847941831", today, 10, max_budget=D("35")),
        "bob": request("req_bob", "tg_bob", "9780349411903", today, 7),
        "charlie": request("req_charlie", "tg_charlie", "9780857197689", today, 14),
    }
