"""Runtime selection for the Reap sandbox service.

Reap is the default and only production-like mode.  The fixture catalog is an
explicit opt-in for UI work before a team receives its sandbox key.
"""

import os
from functools import lru_cache
from typing import Any, Dict, Optional, Sequence

from .models import BookQuery
from .reap import ReapSandboxClient
from .reap_service import ReapMerchantIntelligenceService
from .service import MerchantIntelligenceService, create_demo_service


class MerchantServiceConfigurationError(RuntimeError):
    pass


class MerchantIntelligenceRuntime:
    def __init__(self, service: Any, mode: str) -> None:
        self.service = service
        self.mode = mode

    def compare_offers(
        self,
        query: BookQuery,
        buyer_email: Optional[str] = None,
        shipping_address: Optional[Dict[str, Any]] = None,
        country: str = "SG",
        currency: str = "SGD",
        limit: int = 8,
    ) -> Dict[str, Any]:
        if self.mode == "reap_sandbox":
            return self.service.compare_offers(
                query,
                buyer_email=buyer_email,
                shipping_address=shipping_address,
                country=country,
                currency=currency,
                limit=limit,
            )
        return self.service.compare_offers(query)

    def quote_reap_cart(
        self,
        items: Sequence[Dict[str, Any]],
        buyer_email: str,
        shipping_address: Dict[str, Any],
        offer_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.mode != "reap_sandbox":
            raise MerchantServiceConfigurationError(
                "Reap cart quotes require REAP_API_KEY; demo mode only supports core fixture quotes."
            )
        return self.service.quote_cart(items, buyer_email, shipping_address, offer_code)

    def merchant_catalog(self) -> Any:
        return self.service.merchant_catalog()


@lru_cache(maxsize=1)
def create_runtime() -> MerchantIntelligenceRuntime:
    """Return the process-wide runtime used by every HTTP request.

    Reap cart quotes accept only offer IDs that were verified during an earlier
    offer search.  The Reap service keeps that short-lived verification cache
    in memory, so constructing a fresh service for every request would make a
    valid ``/merchant/offers`` -> ``/merchant/cart-quote`` flow fail.
    """
    api_key = os.getenv("REAP_API_KEY")
    if api_key:
        client = ReapSandboxClient(
            api_key=api_key,
            base_url=os.getenv("REAP_BASE_URL", "https://sg.sandbox.api.reap.global"),
            reap_version=os.getenv("REAP_VERSION", "2025-02-14"),
        )
        return MerchantIntelligenceRuntime(ReapMerchantIntelligenceService(client), "reap_sandbox")

    if os.getenv("BOOKPOOL_DEMO_MODE", "").casefold() in {"1", "true", "yes"}:
        return MerchantIntelligenceRuntime(create_demo_service(), "explicit_demo_fixture")

    raise MerchantServiceConfigurationError(
        "REAP_API_KEY is not configured. Set it for the Reap sandbox, or explicitly set "
        "BOOKPOOL_DEMO_MODE=true for non-payable fixture data."
    )
