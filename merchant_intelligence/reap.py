"""Small, dependency-free client for Reap's Agentic sandbox API.

It deliberately exposes only discovery and quoting.  This component never
handles a raw card number, opens a checkout, or charges an enrollment.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import socket
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


DEFAULT_SANDBOX_URL = "https://sg.sandbox.api.reap.global"
DEFAULT_REAP_VERSION = "2025-02-14"
SANDBOX_HOSTS = {"sg.sandbox.api.reap.global", "sandbox.api.reap.global"}


@dataclass
class ReapApiError(RuntimeError):
    """A concise API error safe to relay to the application boundary."""

    status_code: Optional[int]
    code: str
    message: str
    detail: Any = None

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.message)


class ReapSandboxClient:
    """HTTP client for Reap Agentic product discovery and quotes."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_SANDBOX_URL,
        reap_version: str = DEFAULT_REAP_VERSION,
        timeout_seconds: float = 25.0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("REAP_API_KEY is required for Reap sandbox mode")
        parsed_url = urlparse(base_url)
        if parsed_url.scheme != "https" or parsed_url.hostname not in SANDBOX_HOSTS:
            raise ValueError(
                "Reap merchant intelligence is sandbox-only; use an approved Reap sandbox HTTPS host"
            )
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.reap_version = reap_version
        self.timeout_seconds = timeout_seconds

    def search_products(
        self,
        query: str,
        country: str = "SG",
        currency: str = "SGD",
        limit: int = 10,
        merchant_name: Optional[str] = None,
        available_only: bool = False,
    ) -> Dict[str, Any]:
        """Search Reap's merchant catalog; no merchant checkout is contacted."""

        payload: Dict[str, Any] = {
            "query": query,
            "context": {"country": country, "currency": currency},
            "pagination": {"limit": max(1, min(int(limit), 25))},
        }
        if merchant_name:
            # ONLY is intentionally restrictive: a merchant result must come
            # from the official event allowlist, not merely be preferred.
            payload["merchantPreference"] = {"mode": "ONLY", "merchantName": merchant_name}
        if available_only:
            payload["filters"] = {"availability": "AVAILABLE_ONLY"}
        return self._post("/agentic/products/search", payload)

    def get_product_details(self, product_ids: Sequence[str]) -> Dict[str, Any]:
        if not product_ids:
            return {"products": [], "errors": []}
        # Reap accepts at most ten IDs per documented request.
        products: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        for start in range(0, len(product_ids), 10):
            response = self._post(
                "/agentic/products/details", {"productIds": list(product_ids[start : start + 10])}
            )
            products.extend(response.get("products", []))
            errors.extend(response.get("errors", []))
        return {"products": products, "errors": errors}

    def resolve_variant(self, product_id: str, option_ids: Sequence[str]) -> Dict[str, Any]:
        return self._post(
            "/agentic/products/variant",
            {"productId": product_id, "optionIds": list(option_ids)},
        )

    def create_quote(
        self,
        items: Sequence[Dict[str, Any]],
        email: str,
        shipping_address: Optional[Dict[str, Any]] = None,
        offer_code: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a short-lived sandbox quote, never a payment or checkout."""

        if not email or "@" not in email:
            raise ValueError("A buyer email is required to create a Reap quote")
        if not items:
            raise ValueError("At least one variant is required to create a Reap quote")
        payload: Dict[str, Any] = {"items": list(items), "email": email}
        if shipping_address:
            payload["shippingAddress"] = shipping_address
        if offer_code:
            payload["offerCode"] = offer_code
        return self._post(
            "/agentic/quotes",
            payload,
            idempotency_key=idempotency_key or str(uuid4()),
        )

    def _post(
        self,
        path: str,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
            "Reap-Version": self.reap_version,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                decoded = response.read().decode("utf-8")
                return json.loads(decoded) if decoded else {}
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            parsed = self._parse_error(raw_body)
            raise ReapApiError(exc.code, parsed["code"], parsed["message"], parsed["detail"])
        except (URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            raise ReapApiError(None, "REAP_NETWORK_ERROR", str(reason), None)
        except json.JSONDecodeError as exc:
            raise ReapApiError(None, "REAP_INVALID_RESPONSE", "Reap returned invalid JSON", None) from exc

    @staticmethod
    def _parse_error(body: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return {"code": "REAP_HTTP_ERROR", "message": body or "Reap request failed", "detail": None}
        error = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
        return {
            "code": error.get("code", "REAP_HTTP_ERROR"),
            "message": error.get("message", "Reap request failed"),
            "detail": error.get("detail"),
        }
