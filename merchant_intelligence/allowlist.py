"""The Books & Stationery merchants published for the Reap × 65labs buildathon.

This is an explicit allowlist.  Discovery results from Reap are filtered through
it before they reach the rest of BookPool, so an accidental broad catalog search
cannot lead the agent to recommend an unsupported merchant.
"""

from typing import Dict, Optional, Tuple


OFFICIAL_BOOK_MERCHANTS: Tuple[str, ...] = (
    "atomicbooks.com",
    "baronfig.com",
    "byndartisan.com",
    "kinokuniya.com.sg",
    "komorebistationery.com",
    "mossery.co",
    "popular.com.sg",
    "stationerypal.com",
)

# Reap may return a display name rather than the exact storefront domain.  These
# aliases only canonicalise an approved merchant; they never expand the list.
_ALIASES: Dict[str, str] = {
    "atomicbooks": "atomicbooks.com",
    "atomic books": "atomicbooks.com",
    "baronfig": "baronfig.com",
    "bynd artisan": "byndartisan.com",
    "byndartisan": "byndartisan.com",
    "kinokuniya": "kinokuniya.com.sg",
    "kinokuniya singapore": "kinokuniya.com.sg",
    "books kinokuniya singapore": "kinokuniya.com.sg",
    "komorebi stationery": "komorebistationery.com",
    "komorebi": "komorebistationery.com",
    "mossery": "mossery.co",
    "popular": "popular.com.sg",
    "popular singapore": "popular.com.sg",
    "stationery pal": "stationerypal.com",
    "stationerypal": "stationerypal.com",
}


def canonical_merchant(value: Optional[str]) -> Optional[str]:
    """Return the official domain for a recognised Reap merchant name."""

    if not value:
        return None
    normalized = value.strip().casefold()
    normalized = normalized.removeprefix("https://").removeprefix("http://")
    normalized = normalized.removeprefix("www.").rstrip("/")
    if normalized in OFFICIAL_BOOK_MERCHANTS:
        return normalized
    return _ALIASES.get(normalized)


def is_approved_merchant(value: Optional[str]) -> bool:
    return canonical_merchant(value) is not None
