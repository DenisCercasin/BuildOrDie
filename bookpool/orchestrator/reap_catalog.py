"""Reap sandbox product discovery for the two approved bookstores."""

import asyncio
import re
from dataclasses import dataclass

from app.payments import ProviderError, ReapSandbox, money_minor

MERCHANTS = {
    "Books Kinokuniya Singapore": "kinokuniya.com.sg",
    "Popular Bookstore": "popular.com.sg",
}


def title_matches(requested: str, found: str) -> bool:
    def words(value: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", value.casefold())

    wanted = words(requested)
    actual = words(found)
    return bool(wanted) and actual[: len(wanted)] == wanted


@dataclass(frozen=True)
class ReapMatch:
    merchant_id: str
    title: str
    variant_id: str
    price_minor: int


class ReapCatalog:
    def __init__(self, provider: ReapSandbox):
        self.provider = provider

    async def search(
        self, title: str, isbn: str | None, edition: str | None
    ) -> list[ReapMatch]:
        query = isbn or title
        result = await asyncio.to_thread(
            self.provider.call,
            "POST",
            "/agentic/products/search",
            {
                "query": query,
                "context": {"country": "SG", "currency": "SGD"},
                "filters": {"availability": "AVAILABLE_ONLY"},
                "pagination": {"limit": 20},
            },
        )
        candidates: dict[str, dict] = {}
        for product in result.get("products", []):
            merchant_id = MERCHANTS.get(product.get("merchant", {}).get("name"))
            variant = product.get("previewVariant") or {}
            if (
                not merchant_id
                or not product.get("available")
                or not variant.get("available")
            ):
                continue
            if not title_matches(title, product.get("name", "")):
                continue
            name = f"{product.get('name', '')} {variant.get('name', '')}".lower()
            if re.search(
                r"\b(hindi|chinese|spanish|french|german|tamil|malay) edition\b", name
            ):
                continue
            if edition in {"paperback", "hardcover"} and edition not in name:
                continue
            try:
                price = money_minor(variant["price"])
            except (KeyError, ProviderError):
                continue
            current = candidates.get(merchant_id)
            if current is None or price < current["price"]:
                candidates[merchant_id] = {"product": product, "price": price}
        matches = []
        for merchant_id, candidate in candidates.items():
            product = candidate["product"]
            detail = await asyncio.to_thread(
                self.provider.call,
                "POST",
                "/agentic/products/details",
                {"productIds": [product["id"]]},
            )
            details = next(
                (
                    item
                    for item in detail.get("products", [])
                    if item.get("id") == product["id"]
                ),
                None,
            )
            variant = (
                (details or {}).get("defaultVariant")
                or product.get("previewVariant")
                or {}
            )
            if not variant.get("id") or not variant.get("available"):
                continue
            try:
                price = money_minor(variant["price"])
            except (KeyError, ProviderError):
                continue
            matches.append(
                ReapMatch(merchant_id, product["name"], variant["id"], price)
            )
        return matches
