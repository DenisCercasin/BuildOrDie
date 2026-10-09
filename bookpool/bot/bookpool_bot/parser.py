import json
import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from bookpool_bot.models import BookFormat
from bookpool_bot.utils import parse_amount_minor, parse_deadline


class Intent(StrEnum):
    CREATE_REQUEST = "create_request"
    MODIFY_REQUEST = "modify_request"
    CANCEL_REQUEST = "cancel_request"
    VIEW_REQUESTS = "view_requests"
    VIEW_GROUPS = "view_groups"
    HELP = "help"
    UNKNOWN = "unknown"


class ParsedRequest(BaseModel):
    intent: Intent = Intent.CREATE_REQUEST
    title: str | None = None
    isbn: str | None = None
    author: str | None = None
    edition: str | None = None
    language: str | None = None
    format: BookFormat | None = None
    maximum_budget_minor: int | None = Field(default=None, gt=0)
    latest_delivery_date: date | None = None
    minimum_savings_minor: int | None = Field(default=None, ge=0)
    minimum_savings_percent: Decimal | None = Field(default=None, ge=0, le=100)
    confidence: float = Field(default=1, ge=0, le=1)


def valid_isbn(value: str) -> bool:
    digits = re.sub(r"[-\s]", "", value).upper()
    if len(digits) == 10 and re.fullmatch(r"\d{9}[\dX]", digits):
        return sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits)) % 11 == 0
    if len(digits) == 13 and digits.isdigit():
        return sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(digits)) % 10 == 0
    return False


class DeterministicParser:
    def parse(self, text: str, base: date | None = None) -> ParsedRequest:
        raw = text.strip()
        lower = raw.lower()
        if not raw:
            return ParsedRequest(intent=Intent.UNKNOWN, confidence=0)
        for pattern, intent in [
            (r"^(?:/help|help|how does it work)\??$", Intent.HELP),
            (r"^(?:/myrequests|my requests|show my requests)\??$", Intent.VIEW_REQUESTS),
            (r"^(?:/groups|groups|show groups)\??$", Intent.VIEW_GROUPS),
            (r"^(?:/cancel|cancel|never mind)\??$", Intent.CANCEL_REQUEST),
        ]:
            if re.fullmatch(pattern, lower):
                return ParsedRequest(intent=intent)
        result = ParsedRequest(
            intent=Intent.MODIFY_REQUEST
            if re.match(r"^(actually|change|make it|instead)\b", lower)
            else Intent.CREATE_REQUEST
        )
        isbn_match = re.search(
            r"\b(?:ISBN(?:-1[03])?[:\s]*)?((?:97[89][-\s]?)?\d[-\d\s]{8,16}[\dXx])\b", raw, re.I
        )
        if isbn_match:
            candidate = isbn_match.group(1).strip()
            if valid_isbn(candidate):
                result.isbn = re.sub(r"[-\s]", "", candidate).upper()
            elif "isbn" in lower:
                raise ValueError("That ISBN check digit is invalid. Please check the number.")
        if "paperback" in lower:
            result.format = BookFormat.PAPERBACK
        elif "hardcover" in lower or "hardback" in lower:
            result.format = BookFormat.HARDCOVER
        edition_match = re.search(
            r"\b(\d+(?:st|nd|rd|th) edition|(?:first|second|third|revised) edition)\b",
            raw,
            re.I,
        )
        if edition_match:
            result.edition = edition_match.group(1)
        language_match = re.search(
            r"\b(?:in|language[:\s]+)\s*(English|Chinese|Malay|Tamil)\b", raw, re.I
        )
        if language_match:
            result.language = language_match.group(1).title()
        budget = re.search(
            r"\b(?:under|below|max(?:imum)?(?: budget)?(?: is| of)?|budget(?: is| of)?|up to)\s*(?:S\$|\$|SGD\s*)?\s*(\d+(?:\.\d{1,2})?)",
            raw,
            re.I,
        )
        if budget:
            if re.search(r"\d+\.\d{3,}", raw):
                raise ValueError("Use at most two decimal places for SGD amounts.")
            result.maximum_budget_minor = parse_amount_minor(budget.group(1))
        savings = re.search(
            r"\bsav(?:e|ings?)\s+(?:at least\s*)?(?:S\$|\$|SGD\s*)?\s*(\d+(?:\.\d{1,2})?)(?![\d.]|\s*%)",
            raw,
            re.I,
        )
        if savings:
            result.minimum_savings_minor = parse_amount_minor(savings.group(1))
        elif "any savings" in lower:
            result.minimum_savings_minor = 0
        savings_percent = re.search(
            r"\b(?:sav(?:e|ings?)\s+(?:at least\s*)?|minimum\s+)(\d+(?:\.\d{1,2})?)\s*%",
            raw,
            re.I,
        )
        if savings_percent:
            result.minimum_savings_percent = Decimal(savings_percent.group(1))
        if re.search(r"\b(?:wait(?:ing)?|by|before|until|need it|pick.?up)\b", lower) and re.search(
            r"\b\d+\s*days?\b|\b(?:one|two|a)\s+weeks?\b|20\d{2}-\d{1,2}-\d{1,2}|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}",
            lower,
        ):
            result.latest_delivery_date = parse_deadline(raw, base)
        # Strip preference clauses before treating remaining words as a title.
        title = re.sub(
            r"^(?:i (?:want|need|would like)|find(?: me)?|looking for|get me)\s+",
            "",
            raw,
            flags=re.I,
        )
        title = re.sub(r"^(?:a|an|the)\s+", "", title, flags=re.I)
        title = re.sub(
            r"^(?:(?:a|an|the)\s+)?(?:paperback|hardcover|hardback)\s+copy\s+of\s+",
            "",
            title,
            flags=re.I,
        )
        title = re.split(
            r"[,;]|\b(?:under|below|with a budget|and i can wait|i can wait|by next|before next|by 20\d{2}|before 20\d{2}|by (?:january|february|march|april|may|june|july|august|september|october|november|december)|before (?:january|february|march|april|may|june|july|august|september|october|november|december))\b",
            title,
            maxsplit=1,
            flags=re.I,
        )[0]
        if edition_match:
            title = re.sub(re.escape(edition_match.group(0)), "", title, flags=re.I)
        title = re.sub(r"\b(?:paperback|hardcover|hardback|copy|edition)\b", "", title, flags=re.I)
        title = re.sub(
            r"\s+(?:for|by)\s+(.+)$",
            lambda m: "" if m.group(0).lower().startswith(" for") else m.group(0),
            title,
        )
        author_match = re.search(r"\bby\s+([A-Za-z][A-Za-z .'-]+?)(?:[,;]|$)", raw, re.I)
        if author_match and not re.search(
            r"\bby\s+(?:\d|next\b|monday\b|tuesday\b|wednesday\b|thursday\b|friday\b|saturday\b|sunday\b)",
            author_match.group(0),
            re.I,
        ):
            result.author = author_match.group(1).strip()
            title = re.sub(r"\s+by\s+.*$", "", title, flags=re.I)
        title = re.sub(r"^(?:actually|change|make it|instead)\s+", "", title, flags=re.I).strip(
            " .!?\"'"
        )
        if title.lower() in {"this book", "a book", "the book", "book", "it"}:
            title = ""
        if (
            not re.fullmatch(
                r"(?:my maximum budget.*|i can wait.*|i need it.*|any savings.*)", lower
            )
            and title
            and not result.isbn
        ):
            result.title = title
        if (
            result.intent == Intent.MODIFY_REQUEST
            and result.title is None
            and result.format is None
        ):
            result.confidence = 0.4
        return result


class StructuredProvider(Protocol):
    async def parse(self, text: str) -> ParsedRequest: ...


class OllamaProvider:
    def __init__(self, model: str, base_url: str):
        self.model = model
        self.base_url = base_url

    async def parse(self, text: str) -> ParsedRequest:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/api/generate",
                json={
                    "model": self.model,
                    "stream": False,
                    "format": ParsedRequest.model_json_schema(),
                    "prompt": "Extract only explicitly stated book purchase fields as JSON matching the schema. Never infer a price, author, ISBN, or deadline. User: "
                    + text[:1000],
                },
            )
            response.raise_for_status()
            return ParsedRequest.model_validate_json(response.json()["response"])


class ChatCompletionsProvider:
    """Optional adapter for JSON-capable Chat Completions compatible servers."""

    def __init__(self, model: str, base_url: str, api_key: str):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key

    async def parse(self, text: str) -> ParsedRequest:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": "Return one JSON object matching this schema. Extract only user-supplied book purchase details; never invent authors, ISBNs, prices, dates, or availability. "
                            + json.dumps(ParsedRequest.model_json_schema()),
                        },
                        {"role": "user", "content": text[:1000]},
                    ],
                },
            )
            response.raise_for_status()
            return ParsedRequest.model_validate_json(
                response.json()["choices"][0]["message"]["content"]
            )


class BookRequestParser:
    def __init__(
        self,
        provider: str = "none",
        model: str = "",
        ollama_base_url: str = "http://localhost:11434",
        llm_base_url: str = "",
        llm_api_key: str = "",
        adapter: StructuredProvider | None = None,
    ):
        self.fallback = DeterministicParser()
        self.adapter = adapter
        if self.adapter is None and provider == "ollama" and model:
            self.adapter = OllamaProvider(model, ollama_base_url)
        elif self.adapter is None and provider == "openai_compatible" and model and llm_api_key:
            self.adapter = ChatCompletionsProvider(model, llm_base_url, llm_api_key)

    async def parse(self, text: str, base: date | None = None) -> ParsedRequest:
        if self.adapter is not None:
            try:
                parsed = await self.adapter.parse(text)
                if parsed.confidence >= 0.7:
                    return parsed
            except (httpx.HTTPError, KeyError, ValueError, TypeError, IndexError):
                pass
        return self.fallback.parse(text, base)
