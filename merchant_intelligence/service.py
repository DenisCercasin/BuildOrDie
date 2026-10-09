"""Deterministic matching and cart-pricing service for the demo catalog.

The class is intentionally useful without FastAPI, so Person 3 can import it
directly while optimising a proposed group cart.  In the running app, the same
contract is exposed by :mod:`merchant_intelligence.api`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .catalog import DEMO_LISTINGS, MERCHANTS
from .models import (
    Book,
    BookQuery,
    CartItem,
    Listing,
    StockStatus,
    money_value,
    normalize_isbn,
    normalized_text,
)


class MerchantIntelligenceService:
    """Search a supplied catalog and calculate transparent landed costs."""

    def __init__(self, listings: Sequence[Listing], demo_mode: bool = True) -> None:
        self._listings = tuple(listings)
        self._demo_mode = demo_mode

    @property
    def listings(self) -> Tuple[Listing, ...]:
        return self._listings

    def compare_offers(
        self, query: BookQuery, as_of: Optional[date] = None
    ) -> Dict[str, Any]:
        """Return comparable offers for one requested book.

        ISBN is always authoritative.  Title fallback deliberately requires a
        sufficiently strong title match and honours a requested author, edition,
        or format rather than silently switching editions.
        """

        comparison_date = as_of or date.today()
        scored: List[Tuple[Listing, float, str]] = []
        for listing in self._listings:
            score, method = self._match_listing(listing, query)
            if score <= 0:
                continue
            if not query.include_out_of_stock and not listing.stock_status.purchasable:
                continue
            scored.append((listing, score, method))

        if not scored:
            return {
                "query": query.to_dict(),
                "matched_book": None,
                "offers": [],
                "best_individual_offer_id": None,
                "warnings": self._base_warnings()
                + ["No compatible edition was found in the configured catalog."],
            }

        canonical_book = self._choose_canonical_book(scored)
        offers = [
            self._offer_dict(listing, score, method, query, comparison_date)
            for listing, score, method in scored
            if listing.book.isbn == canonical_book.isbn
        ]
        offers.sort(key=self._offer_sort_key)
        eligible = [offer for offer in offers if offer["eligible_for_purchase"]]
        warnings = self._base_warnings()
        if not eligible:
            warnings.append(
                "Matches were found, but none meet the requested availability, deadline, and budget."
            )

        return {
            "query": query.to_dict(),
            "matched_book": canonical_book.to_dict(),
            "offers": offers,
            "best_individual_offer_id": eligible[0]["offer_id"] if eligible else None,
            "warnings": warnings,
        }

    def quote_cart(
        self,
        merchant_id: str,
        items: Sequence[CartItem],
        as_of: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Quote a same-merchant cart without allocating shared shipping.

        This is the exact seam Person 3 should use for group optimisation: it
        receives a merchant subtotal and one cart-level shipping charge, then
        decides how (or whether) to allocate that saving across buyers.
        """

        comparison_date = as_of or date.today()
        merchant_key = self._canonical_demo_merchant_id(merchant_id)
        merchant = MERCHANTS.get(merchant_key)
        if merchant is None:
            return {
                "merchant_id": merchant_id,
                "purchasable": False,
                "items": [],
                "unavailable_items": [
                    {"reference": item.reference, "reason": "merchant_not_supported"}
                    for item in items
                ],
                "subtotal_sgd": 0.0,
                "shipping": None,
                "tax_sgd": 0.0,
                "total_sgd": None,
                "warnings": ["This demo catalog has no approved merchant with that identifier."],
            }

        merchant_listings = [
            listing for listing in self._listings if listing.merchant.merchant_id == merchant_key
        ]
        lines: List[Dict[str, Any]] = []
        unavailable: List[Dict[str, str]] = []
        subtotal = Decimal("0.00")

        for item in items:
            listing = self._find_cart_listing(merchant_listings, item.reference)
            if listing is None:
                unavailable.append({"reference": item.reference, "reason": "not_found_at_merchant"})
                continue
            line_total = listing.price_sgd * item.quantity
            subtotal += line_total
            lines.append(
                {
                    "offer_id": listing.offer_id,
                    "book": listing.book.to_dict(),
                    "quantity": item.quantity,
                    "unit_price_sgd": money_value(listing.price_sgd),
                    "line_total_sgd": money_value(line_total),
                    "stock_status": listing.stock_status.value,
                    "available": listing.stock_status.purchasable,
                }
            )
            if not listing.stock_status.purchasable:
                unavailable.append({"reference": item.reference, "reason": "out_of_stock"})

        shipping = merchant.shipping_policy.quote(subtotal)
        purchasable = bool(lines) and not unavailable and len(lines) == len(items)
        max_min_days = max((merchant.delivery.min_days for _ in lines), default=0)
        max_max_days = max((merchant.delivery.max_days for _ in lines), default=0)
        total = subtotal + Decimal(str(shipping["fee_sgd"])) if purchasable else None

        return {
            "merchant_id": merchant.merchant_id,
            "merchant": merchant.name,
            "source": "simulated" if self._demo_mode else "catalog",
            "purchasable": purchasable,
            "items": lines,
            "unavailable_items": unavailable,
            "subtotal_sgd": money_value(subtotal),
            "shipping": shipping,
            "tax_sgd": 0.0,
            "total_sgd": money_value(total) if total is not None else None,
            "estimated_delivery_days": {
                "min_days": max_min_days,
                "max_days": max_max_days,
                "earliest_delivery_date": (
                    comparison_date.fromordinal(comparison_date.toordinal() + max_min_days).isoformat()
                ),
                "latest_delivery_date": (
                    comparison_date.fromordinal(comparison_date.toordinal() + max_max_days).isoformat()
                ),
            },
            "warnings": self._base_warnings() if self._demo_mode else [],
        }

    def merchant_catalog(self) -> List[Dict[str, Any]]:
        """Expose demo merchant policy metadata for the UI and optimiser."""

        return [merchant.to_dict() for merchant in MERCHANTS.values()]

    def _match_listing(self, listing: Listing, query: BookQuery) -> Tuple[float, str]:
        book = listing.book
        if query.isbn:
            if book.isbn == query.isbn:
                return 1.0, "exact_isbn"
            return 0.0, "isbn_mismatch"

        title_score = self._text_score(query.title, book.title)
        if title_score < 0.72:
            return 0.0, "title_mismatch"

        author_score: Optional[float] = None
        if query.author:
            author_score = self._text_score(query.author, book.author)
            if author_score < 0.72:
                return 0.0, "author_mismatch"

        if query.format and normalized_text(query.format) != normalized_text(book.format):
            return 0.0, "format_mismatch"
        if query.edition and self._text_score(query.edition, book.edition) < 0.65:
            return 0.0, "edition_mismatch"

        components = [title_score]
        if author_score is not None:
            components.append(author_score)
        if query.format:
            components.append(1.0)
        if query.edition:
            components.append(self._text_score(query.edition, book.edition))
        confidence = sum(components) / len(components)
        return round(min(confidence, 0.99), 3), "title_author_edition_match"

    @staticmethod
    def _text_score(left: Optional[str], right: Optional[str]) -> float:
        left_normalized = normalized_text(left)
        right_normalized = normalized_text(right)
        if not left_normalized or not right_normalized:
            return 0.0
        if left_normalized == right_normalized:
            return 1.0
        left_tokens = set(left_normalized.split())
        right_tokens = set(right_normalized.split())
        if left_tokens and left_tokens.issubset(right_tokens):
            return 0.94
        return SequenceMatcher(None, left_normalized, right_normalized).ratio()

    @staticmethod
    def _choose_canonical_book(scored: Iterable[Tuple[Listing, float, str]]) -> Book:
        grouped: Dict[str, List[Tuple[Listing, float, str]]] = defaultdict(list)
        for candidate in scored:
            grouped[candidate[0].book.isbn].append(candidate)
        _, entries = max(
            grouped.items(),
            key=lambda pair: (len(pair[1]), max(entry[1] for entry in pair[1])),
        )
        return max(entries, key=lambda entry: entry[1])[0].book

    def _offer_dict(
        self,
        listing: Listing,
        confidence: float,
        method: str,
        query: BookQuery,
        comparison_date: date,
    ) -> Dict[str, Any]:
        line_price = listing.price_sgd * query.quantity
        shipping = listing.merchant.shipping_policy.quote(line_price)
        total = line_price + Decimal(str(shipping["fee_sgd"]))
        latest_arrival = comparison_date.fromordinal(
            comparison_date.toordinal() + listing.merchant.delivery.max_days
        )
        meets_deadline = query.required_by is None or latest_arrival <= query.required_by
        meets_budget = query.max_budget_sgd is None or total <= query.max_budget_sgd
        available = listing.stock_status.purchasable
        return {
            "offer_id": listing.offer_id,
            "merchant_id": listing.merchant.merchant_id,
            "merchant": listing.merchant.name,
            "product_url": listing.product_url,
            "book": listing.book.to_dict(),
            "unit_price_sgd": money_value(listing.price_sgd),
            "quantity": query.quantity,
            "item_subtotal_sgd": money_value(line_price),
            "stock_status": listing.stock_status.value,
            "available": available,
            "estimated_delivery_days": listing.merchant.delivery.to_dict(comparison_date),
            "shipping": shipping,
            "tax_sgd": 0.0,
            "total_individual_sgd": money_value(total),
            "constraints": {
                "meets_deadline": meets_deadline,
                "meets_budget": meets_budget,
            },
            "eligible_for_purchase": available and meets_deadline and meets_budget,
            "match": {"method": method, "confidence": confidence},
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "source": listing.source,
        }

    @staticmethod
    def _offer_sort_key(offer: Dict[str, Any]) -> Tuple[int, float, float]:
        return (
            0 if offer["eligible_for_purchase"] else 1,
            offer["total_individual_sgd"],
            -offer["match"]["confidence"],
        )

    @staticmethod
    def _find_cart_listing(listings: Sequence[Listing], reference: str) -> Optional[Listing]:
        normalized_reference = reference.strip()
        try:
            normalized_reference = normalize_isbn(normalized_reference) or normalized_reference
        except ValueError:
            pass
        for listing in listings:
            if listing.offer_id == reference or listing.book.isbn == normalized_reference:
                return listing
        return None

    @staticmethod
    def _canonical_demo_merchant_id(value: str) -> str:
        normalized = value.strip().casefold()
        aliases = {
            "kino": "kinokuniya",
            "kinokuniya singapore": "kinokuniya",
            "kinokuniya.com.sg": "kinokuniya",
            "popular singapore": "popular",
            "popular.com.sg": "popular",
        }
        return aliases.get(normalized, normalized)

    def _base_warnings(self) -> List[str]:
        if not self._demo_mode:
            return []
        return [
            "Demo fixture data only: use the Reap sandbox quote flow before presenting a payable total."
        ]


def create_demo_service() -> MerchantIntelligenceService:
    """Create the deterministic fallback used before a Reap sandbox key is configured."""

    return MerchantIntelligenceService(DEMO_LISTINGS, demo_mode=True)
