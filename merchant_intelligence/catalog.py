"""Small, deterministic catalog for the MVP demo.

Every offer below is deliberately marked ``simulated``.  Prices and inventory
are demo fixtures, not claims about a retailer's live catalog.  The book
identities use ISBN-13 so the rest of the system can be exercised exactly as it
would be with a live provider.
"""

from decimal import Decimal
from typing import Dict, List

from .models import (
    Book,
    DeliveryEstimate,
    Listing,
    Merchant,
    ShippingPolicy,
    StockStatus,
)


KINOKUNIYA = Merchant(
    merchant_id="kinokuniya",
    name="Kinokuniya Singapore",
    shipping_policy=ShippingPolicy(
        fee_sgd=Decimal("4.80"),
        free_over_sgd=Decimal("50.00"),
        policy_note="Standard Singapore delivery is S$4.80 below S$50 and free from S$50.",
    ),
    delivery=DeliveryEstimate(min_days=3, max_days=5),
    source_url="https://kinokuniya.com.sg/",
)

POPULAR = Merchant(
    merchant_id="popular",
    name="POPULAR Singapore",
    shipping_policy=ShippingPolicy(
        fee_sgd=Decimal("4.90"),
        free_over_sgd=Decimal("90.00"),
        policy_note="Standard Singapore delivery is S$4.90 below S$90 and free from S$90.",
    ),
    delivery=DeliveryEstimate(min_days=2, max_days=5),
    source_url="https://popular.com.sg/",
)


ATOMIC_HABITS = Book(
    title="Atomic Habits",
    author="James Clear",
    isbn="9781847941831",
    edition="Paperback edition",
    format="paperback",
)
DEEP_WORK = Book(
    title="Deep Work",
    author="Cal Newport",
    isbn="9781455586691",
    edition="Paperback edition",
    format="paperback",
)
PSYCHOLOGY_OF_MONEY = Book(
    title="The Psychology of Money",
    author="Morgan Housel",
    isbn="9780857197689",
    edition="Paperback edition",
    format="paperback",
)


DEMO_LISTINGS: List[Listing] = [
    Listing(
        offer_id="kinokuniya-9781847941831-paperback",
        merchant=KINOKUNIYA,
        book=ATOMIC_HABITS,
        price_sgd=Decimal("29.90"),
        stock_status=StockStatus.IN_STOCK,
    ),
    Listing(
        offer_id="kinokuniya-9781455586691-paperback",
        merchant=KINOKUNIYA,
        book=DEEP_WORK,
        price_sgd=Decimal("19.90"),
        stock_status=StockStatus.IN_STOCK,
    ),
    Listing(
        offer_id="kinokuniya-9780857197689-paperback",
        merchant=KINOKUNIYA,
        book=PSYCHOLOGY_OF_MONEY,
        price_sgd=Decimal("21.90"),
        stock_status=StockStatus.LOW_STOCK,
    ),
    Listing(
        offer_id="popular-9781847941831-paperback",
        merchant=POPULAR,
        book=ATOMIC_HABITS,
        price_sgd=Decimal("27.90"),
        stock_status=StockStatus.IN_STOCK,
    ),
    Listing(
        offer_id="popular-9781455586691-paperback",
        merchant=POPULAR,
        book=DEEP_WORK,
        price_sgd=Decimal("22.90"),
        stock_status=StockStatus.IN_STOCK,
    ),
    Listing(
        offer_id="popular-9780857197689-paperback",
        merchant=POPULAR,
        book=PSYCHOLOGY_OF_MONEY,
        price_sgd=Decimal("20.90"),
        stock_status=StockStatus.IN_STOCK,
    ),
]


MERCHANTS: Dict[str, Merchant] = {
    KINOKUNIYA.merchant_id: KINOKUNIYA,
    POPULAR.merchant_id: POPULAR,
}
