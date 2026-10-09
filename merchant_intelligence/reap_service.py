"""Reap-backed merchant comparison service.

The Reap sandbox is the source of truth for catalog discovery, variants and
merchant pricing.  This module does not call merchant storefronts directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from threading import RLock
from time import monotonic
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .allowlist import OFFICIAL_BOOK_MERCHANTS, canonical_merchant
from .models import BookQuery, normalized_text
from .reap import ReapApiError, ReapSandboxClient
from .service import MerchantIntelligenceService


class ReapMerchantIntelligenceService:
    """Find approved book offers and obtain merchant-priced Reap quotes."""

    def __init__(self, client: ReapSandboxClient) -> None:
        self.client = client
        # An offer ID must be issued by this process after its result has passed
        # the event allowlist.  It prevents a browser client from quoting an
        # arbitrary Reap variant or mixing merchant carts.
        self._verified_offers: Dict[str, Dict[str, Any]] = {}
        self._offer_lock = RLock()
        self._offer_ttl_seconds = 15 * 60

    def compare_offers(
        self,
        query: BookQuery,
        buyer_email: Optional[str] = None,
        shipping_address: Optional[Dict[str, Any]] = None,
        country: str = "SG",
        currency: str = "SGD",
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Search Reap, constrain results to the event allowlist, and compare.

        When a buyer email and shipping address are supplied, every candidate is
        individually quoted by Reap.  That makes shipping, taxes and total cost
        merchant-authoritative rather than guessed from a retailer policy.
        """

        search_term = self._search_term(query)
        approved_products, warnings = self._search_approved_merchants(
            search_term,
            country,
            currency,
            limit,
            available_only=not query.include_out_of_stock,
        )
        if not approved_products:
            return {
                "query": query.to_dict(),
                "matched_book": None,
                "offers": [],
                "best_individual_offer_id": None,
                "warnings": warnings
                + ["No matching product was returned by an approved event merchant."],
                "source": "reap_sandbox",
            }

        details_response = self.client.get_product_details(
            [product["id"] for product in approved_products if product.get("id")]
        )
        detail_by_id = {
            product.get("id"): product for product in details_response.get("products", []) if product.get("id")
        }
        for error in details_response.get("errors", []):
            warnings.append("Reap could not expand product %s: %s" % (
                error.get("productId", "unknown"), error.get("message", "unknown error")
            ))

        offers: List[Dict[str, Any]] = []
        for product in approved_products:
            detail = detail_by_id.get(product.get("id"))
            if detail is None:
                continue
            offer = self._to_offer(detail, query)
            if offer is None:
                continue
            if buyer_email and shipping_address and offer["eligible_for_quote"]:
                self._attach_individual_quote(offer, buyer_email, shipping_address, query, warnings)
            elif buyer_email and shipping_address:
                warnings.append(
                    "Skipped a Reap quote for %s because its requested identity could not be verified."
                    % offer["offer_id"]
                )
            if offer["eligible_for_quote"]:
                self._remember_offer(offer)
            offers.append(offer)

        offers.sort(key=self._offer_sort_key)
        best = next((offer for offer in offers if offer["eligible_for_purchase"]), None)
        if buyer_email and not shipping_address:
            warnings.append("Provide a shipping address to have Reap calculate shipping and the final total.")
        elif shipping_address and not buyer_email:
            warnings.append("Provide a buyer email to have Reap create merchant-priced quotes.")
        elif not buyer_email and not shipping_address:
            warnings.append(
                "Prices are discovery prices. Supply buyer_email and shipping_address for live Reap quote totals."
            )
        if query.required_by:
            warnings.append(
                "Reap does not return a structured delivery ETA, so deadline-constrained offers require manual confirmation from shipping details."
            )

        return {
            "query": query.to_dict(),
            "matched_book": self._matched_book(query, offers),
            "offers": offers,
            "best_individual_offer_id": best["offer_id"] if best else None,
            "warnings": warnings,
            "source": "reap_sandbox",
        }

    def quote_cart(
        self,
        items: Sequence[Dict[str, Any]],
        buyer_email: str,
        shipping_address: Dict[str, Any],
        offer_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create one Reap quote for a proposed same-merchant group cart.

        The caller supplies offer IDs returned by :meth:`compare_offers`, not
        raw Reap variant IDs.  The service resolves those IDs from a short-lived
        server-side allowlist cache, verifies that every item has one approved
        merchant, then returns actual subtotal, shipping, tax and final amount.
        It never creates a checkout or charge.
        """

        resolved_items, merchant = self._resolve_verified_cart(items)
        quote = self.client.create_quote(resolved_items, buyer_email, shipping_address, offer_code)
        result = self._normalise_quote(quote)
        result["merchant_id"] = merchant
        return result

    @staticmethod
    def merchant_catalog() -> List[Dict[str, Any]]:
        return [
            {
                "merchant_id": domain,
                "merchant": domain,
                "approved_for_event": True,
                "category": "Books & Stationery",
                "source": "Reap × 65labs event merchant allowlist",
            }
            for domain in OFFICIAL_BOOK_MERCHANTS
        ]

    def _search_approved_merchants(
        self,
        search_term: str,
        country: str,
        currency: str,
        total_limit: int,
        available_only: bool,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Search every official book merchant with Reap's restrictive mode.

        The modest per-merchant cap keeps an interactive request responsive and
        avoids treating the sandbox as a bulk catalogue API.
        """

        per_merchant_limit = max(1, min(3, total_limit))
        found: List[Dict[str, Any]] = []
        warnings: List[str] = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(
                    self.client.search_products,
                    search_term,
                    country,
                    currency,
                    per_merchant_limit,
                    merchant,
                    available_only,
                ): merchant
                for merchant in OFFICIAL_BOOK_MERCHANTS
            }
            for future in as_completed(futures):
                merchant = futures[future]
                try:
                    response = future.result()
                except ReapApiError as exc:
                    # A sandbox may not have every listed merchant wired up.
                    # Continue comparing the merchants that do resolve.
                    warnings.append("Reap search unavailable for %s: %s" % (merchant, exc.code))
                    continue
                warnings.extend(response.get("warnings", []))
                for product in response.get("products", []):
                    returned_merchant = canonical_merchant(self._merchant_name(product))
                    if returned_merchant == merchant:
                        found.append(product)
                    elif returned_merchant is not None:
                        warnings.append(
                            "Reap returned %s while searching %s; the result was ignored."
                            % (returned_merchant, merchant)
                        )
        # Product IDs are stable; de-duplicate if Reap returns repeated entries.
        unique = {product.get("id"): product for product in found if product.get("id")}
        return list(unique.values())[: max(1, total_limit)], warnings

    def _remember_offer(self, offer: Dict[str, Any]) -> None:
        with self._offer_lock:
            self._prune_expired_offers()
            self._verified_offers[offer["offer_id"]] = {
                "variant_id": offer["reap_variant_id"],
                "merchant_id": offer["merchant_id"],
                "created_at": monotonic(),
            }

    def _resolve_verified_cart(
        self, items: Sequence[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], str]:
        resolved: List[Dict[str, Any]] = []
        merchants = set()
        with self._offer_lock:
            self._prune_expired_offers()
            for item in items:
                offer_id = item.get("offerId") or item.get("offer_id")
                verified = self._verified_offers.get(offer_id)
                if not verified:
                    raise ValueError(
                        "Unknown or expired offer_id. Search offers again before creating a cart quote."
                    )
                quantity = item.get("quantity", 1)
                if not isinstance(quantity, int) or quantity < 1 or quantity > 20:
                    raise ValueError("Cart item quantity must be an integer between 1 and 20")
                merchants.add(verified["merchant_id"])
                resolved.append({"variantId": verified["variant_id"], "quantity": quantity})
        if len(merchants) != 1:
            raise ValueError("A Reap group cart quote must contain offers from exactly one merchant")
        return resolved, merchants.pop()

    def _prune_expired_offers(self) -> None:
        now = monotonic()
        expired = [
            offer_id
            for offer_id, entry in self._verified_offers.items()
            if now - entry["created_at"] > self._offer_ttl_seconds
        ]
        for offer_id in expired:
            del self._verified_offers[offer_id]

    @staticmethod
    def _search_term(query: BookQuery) -> str:
        # An ISBN is stronger than free text and reduces accidental edition swaps.
        if query.isbn:
            return query.isbn
        parts = [query.title]
        if query.author:
            parts.append(query.author)
        if query.format:
            parts.append(query.format)
        return " ".join(part for part in parts if part)

    @staticmethod
    def _merchant_name(product: Dict[str, Any]) -> str:
        merchant = product.get("merchant") or {}
        return merchant.get("name", "") if isinstance(merchant, dict) else str(merchant)

    def _to_offer(self, detail: Dict[str, Any], query: BookQuery) -> Optional[Dict[str, Any]]:
        merchant_name = self._merchant_name(detail)
        merchant_id = canonical_merchant(merchant_name)
        if merchant_id is None:
            return None
        variant = self._select_variant(detail, query)
        if variant is None or not variant.get("id"):
            return None

        product_name = detail.get("name", "")
        title_confidence = MerchantIntelligenceService._text_score(query.title, product_name)
        if not query.isbn and title_confidence < 0.60:
            return None
        variant_options = variant.get("options") or []
        format_value = self._option_value(variant_options, "format") or self._format_from_name(variant.get("name"))
        edition_value = self._option_value(variant_options, "edition")
        discovered_isbn = self._extract_isbn(
            " ".join(str(value) for value in (detail.get("description"), product_name, variant.get("name")) if value)
        )
        available = bool(variant.get("available", False))
        unit_price = self._normalise_money(variant.get("price"))
        is_currency_match = bool(unit_price and unit_price.get("currency") == "SGD")
        format_matches = (
            not query.format
            or (
                format_value is not None
                and normalized_text(query.format) == normalized_text(format_value)
            )
        )
        edition_matches = (
            not query.edition
            or (
                edition_value is not None
                and self._field_matches(query.edition, edition_value)
            )
        )
        author_matches = (
            not query.author
            or self._field_matches(query.author, "%s %s" % (product_name, detail.get("description") or ""))
        )
        title_matches = (
            (bool(query.isbn) and not query.title)
            or (discovered_isbn == query.isbn if query.isbn else False)
            or title_confidence >= 0.72
        )
        if not title_matches:
            return None
        # Reap returned this product from an exact-ISBN query.  Its product
        # schema may not repeat an ISBN, so pair that catalogue provenance with
        # a title check when one was supplied.  We still distinguish this from
        # an ISBN visible in product data in the response.
        isbn_matches = bool(
            not query.isbn
            or discovered_isbn == query.isbn
            or (query.isbn and discovered_isbn is None and title_matches)
        )
        if discovered_isbn and query.isbn and discovered_isbn == query.isbn:
            isbn_state = "confirmed"
        elif query.isbn and isbn_matches:
            isbn_state = "reap_exact_search_match"
        elif discovered_isbn:
            isbn_state = "discovered_unverified"
        elif query.isbn:
            isbn_state = "requested_not_exposed"
        else:
            isbn_state = "not_requested"
        # An exact ISBN search identifies the author even when Reap's
        # lightweight product schema does not expose that field.  Edition and
        # format remain hard constraints when the requester supplied them.
        identity_isbn_match = bool(query.isbn and isbn_matches)
        author_matches = author_matches or identity_isbn_match
        identity_verified = bool(
            title_matches and author_matches and edition_matches and format_matches and isbn_matches
        )
        eligible_for_quote = available and identity_verified
        return {
            "offer_id": "reap:%s:%s" % (detail.get("id"), variant.get("id")),
            "merchant_id": merchant_id,
            "merchant": merchant_name or merchant_id,
            "reap_product_id": detail.get("id"),
            "reap_variant_id": variant.get("id"),
            "product_url": None,
            "book": {
                "title": product_name,
                "author": query.author,
                "isbn": discovered_isbn,
                "requested_isbn": query.isbn,
                "isbn_verification": isbn_state,
                "edition": edition_value,
                "format": format_value,
            },
            "unit_price": unit_price,
            "unit_price_sgd": unit_price["amount"] if is_currency_match else None,
            "quantity": query.quantity,
            "item_subtotal": self._multiply_money(unit_price, query.quantity),
            "item_subtotal_sgd": (
                unit_price["amount"] * query.quantity if is_currency_match else None
            ),
            "stock_status": "in_stock" if available else "out_of_stock",
            "available": available,
            "estimated_delivery_days": None,
            "shipping": {"status": "requires_reap_quote"},
            "tax": None,
            "total_individual": None,
            "total_individual_sgd": None,
            "quote_status": "not_requested",
            "constraints": {
                "meets_deadline": True if query.required_by is None else False,
                "meets_budget": None,
                "format_matches": format_matches,
                "edition_matches": edition_matches,
                "author_matches": author_matches,
                "title_matches": title_matches,
                "isbn_matches": isbn_matches,
            },
            "identity_verification": "verified" if identity_verified else "requires_confirmation",
            "eligible_for_quote": eligible_for_quote,
            "eligible_for_purchase": False,
            "match": {
                "method": "reap_catalog_%s" % ("isbn_query" if query.isbn else "title_match"),
                "confidence": 1.0 if isbn_state == "confirmed" else round(title_confidence, 3),
            },
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "source": "reap_sandbox",
        }

    def _select_variant(self, detail: Dict[str, Any], query: BookQuery) -> Optional[Dict[str, Any]]:
        default_variant = detail.get("defaultVariant")
        if not isinstance(default_variant, dict):
            return None
        if not query.format and not query.edition:
            return default_variant

        selected_option_ids: List[str] = []
        found_requested_requirement = False
        unresolved_requested_requirement = False
        default_options = {
            normalized_text(option.get("name")): normalized_text(option.get("value"))
            for option in (default_variant.get("options") or [])
            if isinstance(option, dict)
        }
        for option_group in detail.get("options") or []:
            option_name = normalized_text(option_group.get("name"))
            values = option_group.get("values") or []
            requested_value = None
            if "format" in option_name or "binding" in option_name:
                requested_value = query.format
            elif "edition" in option_name:
                requested_value = query.edition
            wanted = normalized_text(requested_value) if requested_value else default_options.get(option_name)
            selected = next(
                (
                    value
                    for value in values
                    if self._field_matches(wanted, value.get("label")) and value.get("available")
                ),
                None,
            )
            if selected is None:
                if requested_value:
                    unresolved_requested_requirement = True
                continue
            selected_option_ids.append(selected.get("optionId"))
            if requested_value:
                found_requested_requirement = True

        if (
            found_requested_requirement
            and not unresolved_requested_requirement
            and selected_option_ids
            and all(selected_option_ids)
        ):
            try:
                return self.client.resolve_variant(detail["id"], selected_option_ids)
            except ReapApiError:
                # A visible default variant remains a useful discovery result; it
                # is marked format_mismatch below if it does not actually match.
                return default_variant
        return default_variant

    @staticmethod
    def _field_matches(requested: Any, observed: Any) -> bool:
        requested_value = normalized_text(str(requested or ""))
        observed_value = normalized_text(str(observed or ""))
        if not requested_value or not observed_value:
            return False
        return (
            requested_value == observed_value
            or requested_value in observed_value
            or observed_value in requested_value
        )

    def _attach_individual_quote(
        self,
        offer: Dict[str, Any],
        buyer_email: str,
        shipping_address: Dict[str, Any],
        query: BookQuery,
        warnings: List[str],
    ) -> None:
        try:
            quote = self.client.create_quote(
                [{"variantId": offer["reap_variant_id"], "quantity": offer["quantity"]}],
                buyer_email,
                shipping_address,
            )
        except (ReapApiError, ValueError) as exc:
            offer["quote_status"] = "failed"
            offer["quote_error"] = str(exc)
            offer["eligible_for_purchase"] = False
            warnings.append("Reap could not quote %s: %s" % (offer["offer_id"], exc))
            return
        normalised = self._normalise_quote(quote)
        total = normalised["total"]
        offer.update(
            {
                "quote_status": "quoted",
                "reap_quote_id": normalised["quote_id"],
                "quote_expires_at": normalised["expires_at"],
                "shipping": normalised["shipping"],
                "tax": normalised["tax"],
                "total_individual": total,
                "total_individual_sgd": (
                    total["amount"] if total and total.get("currency") == "SGD" else None
                ),
                "estimated_delivery": normalised["delivery_details"],
            }
        )
        total_sgd = offer["total_individual_sgd"]
        meets_budget = query.max_budget_sgd is None or (
            total_sgd is not None and total_sgd <= float(query.max_budget_sgd)
        )
        # Delivery information is merchant-specific, unstructured text.  Do
        # not promise a deadline from a phrase like "3-5 days".
        meets_deadline = query.required_by is None
        offer["constraints"]["meets_budget"] = meets_budget
        offer["constraints"]["meets_deadline"] = meets_deadline
        offer["eligible_for_purchase"] = bool(
            offer["eligible_for_quote"]
            and offer["quote_status"] == "quoted"
            and meets_deadline
            and meets_budget
        )

    @staticmethod
    def _normalise_quote(quote: Dict[str, Any]) -> Dict[str, Any]:
        breakdown = quote.get("amountBreakdown") or {}
        shipping_options = quote.get("shippingOptions") or []
        selected_shipping = next(
            (option for option in shipping_options if option.get("selected")),
            shipping_options[0] if shipping_options else None,
        )
        tax = breakdown.get("tax") or {}
        tax_amount = tax.get("amount") if isinstance(tax, dict) else None
        shipping = ReapMerchantIntelligenceService._normalise_money(breakdown.get("shipping"))
        shipping_amount = shipping.get("amount") if shipping else None
        return {
            "quote_id": quote.get("id"),
            "expires_at": quote.get("expiresAt"),
            "subtotal": ReapMerchantIntelligenceService._normalise_money(
                breakdown.get("itemsSubtotal")
            ),
            "shipping": {
                "amount": shipping_amount,
                "currency": shipping.get("currency") if shipping else None,
                "qualifies_for_free_shipping": shipping_amount == 0 if shipping_amount is not None else None,
                "free_shipping_threshold": None,
                "free_shipping_threshold_status": "not_exposed_by_reap",
            },
            "tax": ReapMerchantIntelligenceService._normalise_money(tax_amount),
            "total": ReapMerchantIntelligenceService._normalise_money(
                breakdown.get("finalAmount")
            ),
            "shipping_options": [
                {
                    "id": option.get("id"),
                    "name": option.get("name"),
                    "selected": bool(option.get("selected")),
                    "price": ReapMerchantIntelligenceService._normalise_money(option.get("price")),
                    "details": option.get("details") or [],
                }
                for option in shipping_options
            ],
            "delivery_details": selected_shipping.get("details", []) if selected_shipping else [],
            "source": "reap_sandbox",
            "checkout_created": False,
        }

    @staticmethod
    def _normalise_money(value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict) or value.get("amount") is None:
            return None
        amount = value.get("amount")
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return None
        return {"amount": amount, "currency": value.get("currency")}

    @staticmethod
    def _multiply_money(value: Optional[Dict[str, Any]], quantity: int) -> Optional[Dict[str, Any]]:
        if not value:
            return None
        return {"amount": value["amount"] * quantity, "currency": value.get("currency")}

    @staticmethod
    def _option_value(options: Sequence[Dict[str, Any]], option_name: str) -> Optional[str]:
        wanted = normalized_text(option_name)
        for option in options:
            if wanted in normalized_text(option.get("name")):
                value = option.get("value")
                return str(value) if value is not None else None
        return None

    @staticmethod
    def _format_from_name(value: Any) -> Optional[str]:
        name = normalized_text(str(value or ""))
        for candidate in ("paperback", "hardcover", "hardback", "ebook", "audiobook"):
            if candidate in name:
                return candidate
        return None

    @staticmethod
    def _extract_isbn(value: str) -> Optional[str]:
        for match in re.finditer(r"(?<![0-9])(?:97[89][0-9\s-]{10,16}|[0-9][0-9\s-]{8,14}[0-9Xx])(?![0-9])", value):
            candidate = re.sub(r"[^0-9Xx]", "", match.group(0))
            # Product descriptions can contain a valid-looking number.  Let the
            # shared ISBN validator decide whether it is bibliographically valid.
            try:
                from .models import normalize_isbn

                return normalize_isbn(candidate)
            except ValueError:
                continue
        return None

    @staticmethod
    def _matched_book(query: BookQuery, offers: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not offers:
            return None
        first = offers[0]["book"].copy()
        first["title"] = query.title or first.get("title")
        return first

    @staticmethod
    def _offer_sort_key(offer: Dict[str, Any]) -> Tuple[int, int, float]:
        total = offer.get("total_individual_sgd")
        unit = offer.get("unit_price_sgd")
        amount = total if total is not None else (unit if unit is not None else float("inf"))
        return (
            0 if offer.get("eligible_for_purchase") else 1,
            0 if offer.get("quote_status") == "quoted" else 1,
            float(amount),
        )
