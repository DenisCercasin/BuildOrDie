from datetime import timedelta

import httpx
import pytest
from conftest import FakeCallback, FakeMessage

from bookpool_bot.backend import MockBackendClient
from bookpool_bot.handlers.requests import review_choice, send_live_search
from bookpool_bot.merchant_search import CatalogMatch, MerchantSearchClient, SearchResult
from bookpool_bot.models import BookFormat, BookRequestInput
from bookpool_bot.states import RequestFlow
from bookpool_bot.utils import today


def book() -> BookRequestInput:
    return BookRequestInput(
        title="Atomic Habits",
        format=BookFormat.PAPERBACK,
        maximum_budget_minor=4000,
        latest_delivery_date=today() + timedelta(days=7),
    )


@pytest.mark.asyncio
async def test_live_store_search_displays_only_matching_in_stock_books():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/suggest.json"
        assert request.url.params["q"] == "Atomic Habits"
        if request.url.host == "kinokuniya.com.sg":
            products = [
                {
                    "title": "Atomic Habits : Tiny Changes, Remarkable Results",
                    "handle": "9781847941831",
                    "price": "30.47",
                    "available": True,
                },
                {
                    "title": "The Atomic Habits Workbook",
                    "handle": "workbook",
                    "price": "38.95",
                    "available": True,
                },
            ]
        else:
            products = [
                {
                    "title": "Atomic Habits (Paperback)",
                    "handle": "atomic-habits",
                    "price": "29.99",
                    "available": True,
                },
                {
                    "title": "Atomic Habits (Hardcover)",
                    "handle": "atomic-habits-hardcover",
                    "price": "20.00",
                    "available": True,
                },
                {
                    "title": "Atomic Habits (Paperback)",
                    "handle": "unavailable",
                    "price": "10.00",
                    "available": False,
                },
            ]
        return httpx.Response(200, json={"resources": {"results": {"products": products}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        search = MerchantSearchClient(http_client)
        result = await search.search(book())
        assert [item.price_minor for item in result.matches] == [2999, 3047]
        assert result.matches[0].url == "https://popular.com.sg/products/atomic-habits"
        assert not result.failed_stores
        message = FakeMessage()
        await send_live_search(message, book(), search)
        shown = message.sent[0][0]
        assert "S$29.99" in shown
        assert "Delivery charges" in shown
        assert "you do not need to choose a listing" in shown
        assert "Automatic group matching is not active yet" in shown
        assert "Workbook" not in shown


@pytest.mark.asyncio
async def test_live_store_search_reports_partial_failure():
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kinokuniya.com.sg":
            return httpx.Response(503)
        return httpx.Response(200, json={"resources": {"results": {"products": []}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        result = await MerchantSearchClient(http_client).search(book())
        assert result.matches == []
        assert result.failed_stores == ["Kinokuniya"]


@pytest.mark.asyncio
async def test_start_searching_saves_request_then_shows_live_prices(state):
    class LiveBackend(MockBackendClient):
        supports_live_search = True

    class StubSearch:
        async def search(self, request):
            assert request.title == "Atomic Habits"
            return SearchResult(
                [
                    CatalogMatch(
                        "Kinokuniya",
                        "Atomic Habits",
                        3047,
                        "https://kinokuniya.com.sg/products/9781847941831",
                    )
                ],
                [],
            )

    backend = LiveBackend()
    await state.set_state(RequestFlow.review)
    await state.set_data({"draft": book().model_dump(mode="json")})
    callback = FakeCallback("r:confirm")
    await review_choice(callback, state, backend, StubSearch())
    assert callback.answered
    assert len(await backend.list_book_requests(123)) == 1
    assert "Request registered" in callback.message.sent[0][0]
    assert "S$30.47" in callback.message.sent[1][0]
