from datetime import date

import httpx
import pytest
import respx

from bookpool_bot.parser import BookRequestParser, DeterministicParser, Intent, valid_isbn
from bookpool_bot.utils import money, parse_amount_minor, parse_deadline


def test_extract_complete_request():
    parsed = DeterministicParser().parse(
        "I want Atomic Habits, paperback, under S$35, and I can wait 10 days.",
        date(2026, 10, 9),
    )
    assert parsed.title == "Atomic Habits"
    assert parsed.format == "paperback"
    assert parsed.maximum_budget_minor == 3500
    assert parsed.latest_delivery_date == date(2026, 10, 19)


def test_author_and_uncertain_date_not_invented():
    parser = DeterministicParser()
    assert parser.parse("I want Atomic Habits by James Clear").author == "James Clear"
    result = parser.parse("I need Deep Work by next Friday")
    assert result.title == "Deep Work"
    assert result.author is None
    assert result.latest_delivery_date is None


def test_isbn_and_intents():
    assert valid_isbn("9780141036144")
    assert not valid_isbn("9780141036145")
    assert DeterministicParser().parse("my requests").intent == Intent.VIEW_REQUESTS
    with pytest.raises(ValueError, match="ISBN"):
        DeterministicParser().parse("ISBN 9780141036145")


def test_deadline_and_money():
    assert parse_deadline("two weeks", date(2026, 10, 9)) == date(2026, 10, 23)
    assert parse_deadline("October 25", date(2026, 10, 9)) == date(2026, 10, 25)
    with pytest.raises(ValueError, match="passed"):
        parse_deadline("2026-10-08", date(2026, 10, 9))
    with pytest.raises(ValueError):
        parse_deadline("2026-02-30", date(2026, 1, 1))
    assert parse_amount_minor("S$35.90") == 3590
    assert money(3190) == "S$31.90"
    with pytest.raises(ValueError):
        parse_amount_minor("0")
    with pytest.raises(ValueError, match="decimal"):
        parse_amount_minor("S$35.999")


def test_percentage_savings():
    parsed = DeterministicParser().parse("I want Dune, save at least 10%")
    assert parsed.title == "Dune"
    assert parsed.minimum_savings_percent == 10
    assert parsed.minimum_savings_minor is None


def test_generic_book_and_explicit_details():
    parser = DeterministicParser()
    generic = parser.parse(
        "I want this book, but I don't want to wait more than 5 days", date(2026, 10, 9)
    )
    assert generic.title is None
    assert generic.latest_delivery_date == date(2026, 10, 14)
    detailed = parser.parse("I want Dune, 2nd edition, in English, maximum budget is 30 dollars")
    assert detailed.title == "Dune"
    assert detailed.edition == "2nd edition"
    assert detailed.language == "English"
    assert detailed.maximum_budget_minor == 3000


@pytest.mark.asyncio
async def test_llm_unavailable_falls_back():
    parser = BookRequestParser("ollama", "missing-model", "http://127.0.0.1:1")
    result = await parser.parse("Atomic Habits")
    assert result.title == "Atomic Habits"


@pytest.mark.asyncio
async def test_external_structured_parser_and_fallback():
    parser = BookRequestParser(
        "openai_compatible", "test-model", llm_base_url="https://llm.example/v1", llm_api_key="test"
    )
    async with respx.mock:
        route = respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"title":"Dune","intent":"create_request"}'}}
                    ]
                },
            )
        )
        assert (await parser.parse("Dune")).title == "Dune"
        assert route.calls[0].request.headers["authorization"] == "Bearer test"
        route.mock(return_value=httpx.Response(503))
        assert (await parser.parse("Deep Work")).title == "Deep Work"
