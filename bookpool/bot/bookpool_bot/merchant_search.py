"""Live catalog discovery from BookPool's two approved Singapore bookstores.

These are displayed product prices, not delivered checkout quotes. Search results
must not be written as backend offers until delivery and fees are verified.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from bookpool_bot.models import BookFormat, BookRequestInput

log = logging.getLogger(__name__)

STORES = (
    ("Kinokuniya", "https://kinokuniya.com.sg"),
    ("POPULAR", "https://popular.com.sg"),
)
HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
WORDS = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class CatalogMatch:
    merchant: str
    title: str
    price_minor: int
    url: str


@dataclass(frozen=True)
class SearchResult:
    matches: list[CatalogMatch]
    failed_stores: list[str]


class MerchantSearchClient:
    """Queries allowlisted storefront search endpoints; never estimates shipping."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=8, follow_redirects=False)
        self.owns_client = client is None

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def search(self, request: BookRequestInput) -> SearchResult:
        query = request.isbn or request.title
        if not query:
            return SearchResult([], [])
        results = await asyncio.gather(
            *(self._search_store(name, base, query, request) for name, base in STORES),
            return_exceptions=True,
        )
        matches: list[CatalogMatch] = []
        failed: list[str] = []
        for (name, _), result in zip(STORES, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                log.warning("merchant_search_failed store=%s error=%s", name, result)
                failed.append(name)
            else:
                matches.extend(result)
        return SearchResult(sorted(matches, key=lambda match: match.price_minor)[:6], failed)

    async def _search_store(
        self, name: str, base: str, query: str, request: BookRequestInput
    ) -> list[CatalogMatch]:
        response = await self.client.get(
            f"{base}/search/suggest.json",
            params={"q": query, "resources[type]": "product", "resources[limit]": 8},
        )
        response.raise_for_status()
        products = response.json()["resources"]["results"]["products"]
        if not isinstance(products, list):
            raise ValueError("Store returned an invalid product list")
        matches = []
        for product in products:
            if not isinstance(product, dict) or not product.get("available"):
                continue
            title = product.get("title")
            handle = product.get("handle")
            if not isinstance(title, str) or not isinstance(handle, str):
                continue
            if not HANDLE.fullmatch(handle) or not self._matches(title, handle, query, request):
                continue
            try:
                price = Decimal(str(product["price"]))
                cents = price * 100
                if price <= 0 or cents != cents.to_integral_value():
                    continue
            except (KeyError, InvalidOperation, ValueError):
                continue
            matches.append(CatalogMatch(name, title, int(cents), f"{base}/products/{handle}"))
        return matches

    @staticmethod
    def _matches(title: str, handle: str, query: str, request: BookRequestInput) -> bool:
        if request.isbn and query == request.isbn:
            if request.isbn not in handle and request.isbn not in title:
                return False
        else:
            search_words = set(WORDS.findall(query.lower()))
            title_words = set(WORDS.findall(title.lower()))
            if not search_words or not search_words <= title_words:
                return False
            # A companion workbook is not the requested main book.
            if "workbook" in title_words and "workbook" not in search_words:
                return False
        if request.format == BookFormat.PAPERBACK and "hardcover" in title.lower():
            return False
        if request.format == BookFormat.HARDCOVER and "paperback" in title.lower():
            return False
        return True
