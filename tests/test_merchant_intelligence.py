"""Fast, dependency-free checks for BookPool merchant intelligence."""

import unittest
from datetime import date
import socket
from unittest.mock import patch

from merchant_intelligence.models import BookQuery, CartItem, normalize_isbn
from merchant_intelligence.reap import ReapApiError, ReapSandboxClient
from merchant_intelligence.reap_service import ReapMerchantIntelligenceService
from merchant_intelligence.service import create_demo_service


class DemoCatalogTests(unittest.TestCase):
    def setUp(self):
        self.service = create_demo_service()

    def test_isbn_10_is_normalized_to_isbn_13(self):
        self.assertEqual(normalize_isbn("0735211299"), "9780735211292")

    def test_exact_isbn_precedes_title_text(self):
        comparison = self.service.compare_offers(
            BookQuery(title="completely unrelated words", isbn="9781847941831")
        )
        self.assertEqual(comparison["matched_book"]["isbn"], "9781847941831")
        self.assertEqual(len(comparison["offers"]), 2)
        self.assertTrue(all(offer["match"]["method"] == "exact_isbn" for offer in comparison["offers"]))

    def test_title_author_and_format_do_not_cross_match_editions(self):
        comparison = self.service.compare_offers(
            BookQuery(title="Atomic Habits", author="James Clear", format="hardcover")
        )
        self.assertIsNone(comparison["matched_book"])
        self.assertEqual(comparison["offers"], [])

    def test_individual_offers_are_sorted_by_landed_cost(self):
        comparison = self.service.compare_offers(
            BookQuery(title="Atomic Habits", author="James Clear", format="paperback")
        )
        self.assertEqual(comparison["best_individual_offer_id"], "popular-9781847941831-paperback")
        self.assertEqual(comparison["offers"][0]["total_individual_sgd"], 32.80)
        self.assertEqual(comparison["offers"][1]["total_individual_sgd"], 34.70)

    def test_cart_boundary_unlocks_free_shipping(self):
        below_threshold = self.service.quote_cart(
            "kinokuniya",
            [CartItem("9781847941831"), CartItem("9781455586691")],
        )
        free_shipping = self.service.quote_cart(
            "kinokuniya",
            [
                CartItem("9781847941831"),
                CartItem("9781455586691"),
                CartItem("9780857197689"),
            ],
        )
        self.assertEqual(below_threshold["subtotal_sgd"], 49.80)
        self.assertEqual(below_threshold["shipping"]["fee_sgd"], 4.80)
        self.assertEqual(free_shipping["subtotal_sgd"], 71.70)
        self.assertEqual(free_shipping["shipping"]["fee_sgd"], 0.0)
        self.assertTrue(free_shipping["purchasable"])


class ReapClientTests(unittest.TestCase):
    def test_search_uses_restrictive_merchant_preference(self):
        client = ReapSandboxClient("sandbox-key")
        captured = {}

        def fake_post(path, payload, idempotency_key=None):
            captured["path"] = path
            captured["payload"] = payload
            captured["idempotency_key"] = idempotency_key
            return {"products": []}

        client._post = fake_post  # type: ignore[assignment]
        client.search_products(
            "Atomic Habits",
            merchant_name="kinokuniya.com.sg",
            available_only=True,
        )
        self.assertEqual(captured["path"], "/agentic/products/search")
        self.assertEqual(
            captured["payload"]["merchantPreference"],
            {"mode": "ONLY", "merchantName": "kinokuniya.com.sg"},
        )
        self.assertEqual(captured["payload"]["filters"], {"availability": "AVAILABLE_ONLY"})

    def test_socket_timeout_becomes_a_safe_reap_error(self):
        client = ReapSandboxClient("sandbox-key", timeout_seconds=0.01)
        with patch("merchant_intelligence.reap.urlopen", side_effect=socket.timeout("timed out")):
            with self.assertRaises(ReapApiError) as raised:
                client.search_products("Atomic Habits")
        self.assertEqual(raised.exception.code, "REAP_NETWORK_ERROR")


class FakeReapClient:
    def __init__(self, description="Atomic Habits by James Clear. ISBN 978-1847941831", quote_error=False):
        self.searched_merchants = []
        self.quoted_items = None
        self.description = description
        self.quote_error = quote_error

    def search_products(
        self, query, country, currency, limit, merchant_name=None, available_only=False
    ):
        self.searched_merchants.append(merchant_name)
        if merchant_name == "kinokuniya.com.sg":
            return {
                "products": [
                    {
                        "id": "product-atomic",
                        "merchant": {"name": "kinokuniya.com.sg"},
                        "name": "Atomic Habits",
                    }
                ],
                "warnings": [],
            }
        return {"products": [], "warnings": []}

    def get_product_details(self, product_ids):
        return {
            "products": [
                {
                    "id": "product-atomic",
                    "merchant": {"name": "kinokuniya.com.sg"},
                    "name": "Atomic Habits",
                    "description": self.description,
                    "options": [],
                    "defaultVariant": {
                        "id": "variant-atomic",
                        "name": "Paperback",
                        "price": {"amount": 29.90, "currency": "SGD"},
                        "available": True,
                        "requiresShipping": True,
                        "options": [],
                    },
                }
            ],
            "errors": [],
        }

    def create_quote(self, items, email, shipping_address, offer_code=None, idempotency_key=None):
        if self.quote_error:
            raise ReapApiError(503, "AGENTIC_SERVICE_UNAVAILABLE", "sandbox unavailable")
        self.quoted_items = items
        return {
            "id": "quote-1",
            "expiresAt": "2026-10-09T12:00:00Z",
            "shippingOptions": [
                {
                    "id": "standard",
                    "name": "Standard",
                    "selected": True,
                    "price": {"amount": 4.8, "currency": "SGD"},
                    "details": [{"key": "estimated_delivery", "value": "3-5 days"}],
                }
            ],
            "amountBreakdown": {
                "itemsSubtotal": {"amount": 29.9, "currency": "SGD"},
                "shipping": {"amount": 4.8, "currency": "SGD"},
                "tax": {"amount": {"amount": 0, "currency": "SGD"}},
                "finalAmount": {"amount": 34.7, "currency": "SGD"},
            },
        }


class ReapServiceTests(unittest.TestCase):
    def test_offer_is_discovered_from_allowlisted_reap_merchant_and_quoted(self):
        client = FakeReapClient()
        service = ReapMerchantIntelligenceService(client)  # type: ignore[arg-type]
        result = service.compare_offers(
            BookQuery(title="Atomic Habits", author="James Clear", isbn="9781847941831"),
            buyer_email="buyer@example.com",
            shipping_address={
                "firstName": "Avery",
                "lastName": "Tan",
                "phone": "+6581234567",
                "addressLine1": "1 Example Road",
                "city": "Singapore",
                "postalCode": "123456",
                "country": "SG",
            },
        )
        self.assertEqual(set(client.searched_merchants), {
            "atomicbooks.com",
            "baronfig.com",
            "byndartisan.com",
            "kinokuniya.com.sg",
            "komorebistationery.com",
            "mossery.co",
            "popular.com.sg",
            "stationerypal.com",
        })
        self.assertEqual(len(result["offers"]), 1)
        offer = result["offers"][0]
        self.assertEqual(offer["merchant_id"], "kinokuniya.com.sg")
        self.assertEqual(offer["book"]["isbn_verification"], "confirmed")
        self.assertEqual(offer["quote_status"], "quoted")
        self.assertEqual(offer["total_individual_sgd"], 34.7)
        self.assertEqual(client.quoted_items, [{"variantId": "variant-atomic", "quantity": 1}])
        group_quote = service.quote_cart(
            [{"offerId": offer["offer_id"], "quantity": 2}],
            "buyer@example.com",
            {
                "firstName": "Avery",
                "lastName": "Tan",
                "phone": "+6581234567",
                "addressLine1": "1 Example Road",
                "city": "Singapore",
                "country": "SG",
            },
        )
        self.assertEqual(group_quote["merchant_id"], "kinokuniya.com.sg")
        self.assertEqual(client.quoted_items, [{"variantId": "variant-atomic", "quantity": 2}])

    def test_cart_quote_rejects_unverified_variant_id(self):
        service = ReapMerchantIntelligenceService(FakeReapClient())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "Unknown or expired offer_id"):
            service.quote_cart(
                [{"variantId": "someone-elses-variant", "quantity": 1}],
                "buyer@example.com",
                {
                    "firstName": "Avery",
                    "lastName": "Tan",
                    "phone": "+6581234567",
                    "addressLine1": "1 Example Road",
                    "city": "Singapore",
                    "country": "SG",
                },
            )

    def test_mismatched_exposed_isbn_is_discovery_only(self):
        service = ReapMerchantIntelligenceService(
            FakeReapClient(description="Atomic Habits by James Clear. ISBN 978-0735211292")
        )  # type: ignore[arg-type]
        result = service.compare_offers(
            BookQuery(title="Atomic Habits", author="James Clear", isbn="9781847941831"),
            buyer_email="buyer@example.com",
            shipping_address={
                "firstName": "Avery", "lastName": "Tan", "phone": "+6581234567",
                "addressLine1": "1 Example Road", "city": "Singapore", "country": "SG",
            },
        )
        offer = result["offers"][0]
        self.assertEqual(offer["identity_verification"], "requires_confirmation")
        self.assertFalse(offer["eligible_for_quote"])
        self.assertFalse(offer["eligible_for_purchase"])
        self.assertEqual(offer["quote_status"], "not_requested")
        self.assertIsNone(result["best_individual_offer_id"])

    def test_exact_reap_isbn_search_can_quote_when_product_schema_omits_isbn(self):
        service = ReapMerchantIntelligenceService(
            FakeReapClient(description="Atomic Habits by James Clear")
        )  # type: ignore[arg-type]
        result = service.compare_offers(
            BookQuery(title="Atomic Habits", author="James Clear", isbn="9781847941831"),
            buyer_email="buyer@example.com",
            shipping_address={
                "firstName": "Avery", "lastName": "Tan", "phone": "+6581234567",
                "addressLine1": "1 Example Road", "city": "Singapore", "country": "SG",
            },
        )
        offer = result["offers"][0]
        self.assertEqual(offer["book"]["isbn_verification"], "reap_exact_search_match")
        self.assertEqual(offer["quote_status"], "quoted")
        self.assertTrue(offer["eligible_for_purchase"])

    def test_requested_edition_mismatch_is_not_quoted(self):
        service = ReapMerchantIntelligenceService(FakeReapClient())  # type: ignore[arg-type]
        result = service.compare_offers(
            BookQuery(
                title="Atomic Habits",
                author="James Clear",
                isbn="9781847941831",
                edition="Second edition",
            ),
            buyer_email="buyer@example.com",
            shipping_address={
                "firstName": "Avery", "lastName": "Tan", "phone": "+6581234567",
                "addressLine1": "1 Example Road", "city": "Singapore", "country": "SG",
            },
        )
        offer = result["offers"][0]
        self.assertFalse(offer["constraints"]["edition_matches"])
        self.assertFalse(offer["eligible_for_quote"])
        self.assertEqual(offer["quote_status"], "not_requested")

    def test_quote_failure_is_never_purchase_eligible(self):
        service = ReapMerchantIntelligenceService(FakeReapClient(quote_error=True))  # type: ignore[arg-type]
        result = service.compare_offers(
            BookQuery(title="Atomic Habits", author="James Clear", isbn="9781847941831"),
            buyer_email="buyer@example.com",
            shipping_address={
                "firstName": "Avery", "lastName": "Tan", "phone": "+6581234567",
                "addressLine1": "1 Example Road", "city": "Singapore", "country": "SG",
            },
        )
        offer = result["offers"][0]
        self.assertEqual(offer["quote_status"], "failed")
        self.assertFalse(offer["eligible_for_purchase"])
        self.assertIsNone(result["best_individual_offer_id"])

    def test_deadline_is_not_eligible_without_structured_eta(self):
        service = ReapMerchantIntelligenceService(FakeReapClient())  # type: ignore[arg-type]
        result = service.compare_offers(
            BookQuery(
                title="Atomic Habits",
                author="James Clear",
                isbn="9781847941831",
                required_by=date(2026, 10, 12),
            ),
            buyer_email="buyer@example.com",
            shipping_address={
                "firstName": "Avery", "lastName": "Tan", "phone": "+6581234567",
                "addressLine1": "1 Example Road", "city": "Singapore", "country": "SG",
            },
        )
        offer = result["offers"][0]
        self.assertEqual(offer["quote_status"], "quoted")
        self.assertFalse(offer["constraints"]["meets_deadline"])
        self.assertFalse(offer["eligible_for_purchase"])
        self.assertIsNone(result["best_individual_offer_id"])


if __name__ == "__main__":
    unittest.main()
