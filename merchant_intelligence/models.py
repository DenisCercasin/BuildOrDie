"""Framework-independent data models and normalisation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
import re
import unicodedata
from typing import Any, Dict, List, Optional


MONEY_PLACES = Decimal("0.01")


def as_money(value: Any) -> Decimal:
    """Convert a money-like value to a two-decimal SGD Decimal."""

    try:
        return Decimal(str(value)).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Expected a valid money amount") from exc


def money_value(value: Decimal) -> float:
    """JSON-friendly representation which never exposes floating arithmetic."""

    return float(value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP))


def normalized_text(value: Optional[str]) -> str:
    """Normalise human-entered book text without losing the original display text."""

    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value.casefold()))


def normalize_isbn(value: Optional[str]) -> Optional[str]:
    """Return a validated ISBN-13, converting ISBN-10 inputs when necessary.

    ``None`` and an empty string stay empty.  A malformed ISBN raises a
    ValueError so callers do not silently compare the wrong book.
    """

    if value is None or not str(value).strip():
        return None

    compact = re.sub(r"[^0-9Xx]", "", str(value))
    if len(compact) == 10:
        checksum = sum(
            (10 - index) * (10 if char in ("X", "x") else int(char))
            for index, char in enumerate(compact)
        )
        if checksum % 11:
            raise ValueError("ISBN-10 checksum is invalid")
        prefix = "978" + compact[:9]
        check_digit = (10 - (sum(
            int(char) * (1 if index % 2 == 0 else 3)
            for index, char in enumerate(prefix)
        ) % 10)) % 10
        return prefix + str(check_digit)

    if len(compact) != 13 or not compact.isdigit():
        raise ValueError("ISBN must be a valid ISBN-10 or ISBN-13")

    expected = (10 - (sum(
        int(char) * (1 if index % 2 == 0 else 3)
        for index, char in enumerate(compact[:12])
    ) % 10)) % 10
    if expected != int(compact[-1]):
        raise ValueError("ISBN-13 checksum is invalid")
    return compact


class StockStatus(str, Enum):
    IN_STOCK = "in_stock"
    LOW_STOCK = "low_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"

    @property
    def purchasable(self) -> bool:
        return self in (StockStatus.IN_STOCK, StockStatus.LOW_STOCK, StockStatus.PREORDER)


@dataclass(frozen=True)
class Book:
    title: str
    author: str
    isbn: str
    edition: str
    format: str

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("A book needs a title")
        normalized_isbn = normalize_isbn(self.isbn)
        if normalized_isbn is None:
            raise ValueError("A book needs an ISBN")
        object.__setattr__(self, "isbn", normalized_isbn)
        object.__setattr__(self, "format", self.format.strip().casefold())

    def to_dict(self) -> Dict[str, str]:
        return {
            "title": self.title,
            "author": self.author,
            "isbn": self.isbn,
            "edition": self.edition,
            "format": self.format,
        }


@dataclass(frozen=True)
class DeliveryEstimate:
    min_days: int
    max_days: int

    def __post_init__(self) -> None:
        if self.min_days < 0 or self.max_days < self.min_days:
            raise ValueError("Delivery days must be a valid range")

    def to_dict(self, as_of: date) -> Dict[str, Any]:
        return {
            "min_days": self.min_days,
            "max_days": self.max_days,
            "earliest_delivery_date": (as_of + timedelta(days=self.min_days)).isoformat(),
            "latest_delivery_date": (as_of + timedelta(days=self.max_days)).isoformat(),
        }


@dataclass(frozen=True)
class ShippingPolicy:
    fee_sgd: Decimal
    free_over_sgd: Optional[Decimal]
    policy_note: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "fee_sgd", as_money(self.fee_sgd))
        if self.free_over_sgd is not None:
            object.__setattr__(self, "free_over_sgd", as_money(self.free_over_sgd))

    def shipping_fee(self, subtotal_sgd: Decimal) -> Decimal:
        if self.free_over_sgd is not None and subtotal_sgd >= self.free_over_sgd:
            return Decimal("0.00")
        return self.fee_sgd

    def quote(self, subtotal_sgd: Decimal) -> Dict[str, Any]:
        fee = self.shipping_fee(subtotal_sgd)
        return {
            "fee_sgd": money_value(fee),
            "standard_fee_sgd": money_value(self.fee_sgd),
            "free_over_sgd": (
                money_value(self.free_over_sgd) if self.free_over_sgd is not None else None
            ),
            "qualifies_for_free_shipping": fee == Decimal("0.00"),
            "amount_to_free_shipping_sgd": (
                money_value(max(Decimal("0.00"), self.free_over_sgd - subtotal_sgd))
                if self.free_over_sgd is not None
                else None
            ),
            "policy_note": self.policy_note,
        }


@dataclass(frozen=True)
class Merchant:
    merchant_id: str
    name: str
    shipping_policy: ShippingPolicy
    delivery: DeliveryEstimate
    source_url: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "merchant": self.name,
            "shipping_policy": self.shipping_policy.quote(Decimal("0.00")),
            "estimated_delivery_days": {
                "min_days": self.delivery.min_days,
                "max_days": self.delivery.max_days,
            },
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class Listing:
    offer_id: str
    merchant: Merchant
    book: Book
    price_sgd: Decimal
    stock_status: StockStatus
    product_url: Optional[str] = None
    source: str = "simulated"

    def __post_init__(self) -> None:
        object.__setattr__(self, "price_sgd", as_money(self.price_sgd))


@dataclass
class BookQuery:
    title: str = ""
    author: Optional[str] = None
    isbn: Optional[str] = None
    edition: Optional[str] = None
    format: Optional[str] = None
    quantity: int = 1
    destination: str = "Singapore"
    required_by: Optional[date] = None
    max_budget_sgd: Optional[Decimal] = None
    include_out_of_stock: bool = True

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        self.author = self.author.strip() if self.author else None
        self.edition = self.edition.strip() if self.edition else None
        self.format = self.format.strip().casefold() if self.format else None
        self.destination = self.destination.strip() or "Singapore"
        self.isbn = normalize_isbn(self.isbn)
        if not self.title and not self.isbn:
            raise ValueError("Provide a title or ISBN")
        if self.quantity < 1:
            raise ValueError("Quantity must be at least 1")
        if self.max_budget_sgd is not None:
            self.max_budget_sgd = as_money(self.max_budget_sgd)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title or None,
            "author": self.author,
            "isbn": self.isbn,
            "edition": self.edition,
            "format": self.format,
            "quantity": self.quantity,
            "destination": self.destination,
            "required_by": self.required_by.isoformat() if self.required_by else None,
            "max_budget_sgd": money_value(self.max_budget_sgd) if self.max_budget_sgd else None,
        }


@dataclass(frozen=True)
class CartItem:
    """An item identified by an offer ID or a canonical ISBN."""

    reference: str
    quantity: int = 1

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValueError("Cart item reference cannot be empty")
        if self.quantity < 1:
            raise ValueError("Cart item quantity must be at least 1")
