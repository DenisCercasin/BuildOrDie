from decimal import Decimal, InvalidOperation
from urllib.parse import quote
import httpx


class ProviderError(Exception):
    def __init__(self, code, uncertain=False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


def money_minor(value, currency="SGD"):
    """Reap Amount examples use major currency units; internal accounting uses cents."""
    if not isinstance(value, dict) or value.get("currency") != currency:
        raise ProviderError("CURRENCY_MISMATCH")
    try:
        amount = Decimal(str(value["amount"])) * 100
        if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
            raise ValueError()
        return int(amount)
    except (KeyError, ValueError, InvalidOperation):
        raise ProviderError("INVALID_PROVIDER_AMOUNT")


class ReapSandbox:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    def call(self, method, path, payload=None, key=None):
        headers = {"Authorization": f"Bearer {self.settings.reap_api_key}",
                   "Reap-Version": self.settings.reap_version}
        if key:
            headers["Idempotency-Key"] = key
        try:
            with httpx.Client(timeout=20, transport=self.transport, follow_redirects=False) as client:
                result = client.request(method, self.settings.reap_base_url + path,
                                        json=payload, headers=headers)
            if result.status_code >= 400:
                # Do not leak provider response bodies, credentials or personal data.
                # Only documented validation statuses are definite non-execution.
                raise ProviderError(f"REAP_HTTP_{result.status_code}",
                                    uncertain=result.status_code >= 500 or result.status_code == 409)
            return result.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderError("REAP_NETWORK_OR_RESPONSE_ERROR", uncertain=True)

    def get_quote(self, quote_id):
        return self.call("GET", f"/agentic/quotes/{quote(quote_id, safe='')}")

    def get_enrollment(self, enrollment_id):
        return self.call("GET", f"/agentic/enrollments/{quote(enrollment_id, safe='')}")

    def create_enrollment(self, user_id, email, return_url, key):
        return self.call("POST", "/agentic/enrollments", {
            "source": "EXTERNAL", "owner": {
                "type": "CLIENT_REFERENCE", "id": user_id, "email": email},
            "presentation": {"type": "REDIRECT", "returnUrl": return_url}}, key)

    def create_quote(self, items, email, address, offer_code, key):
        payload = {"items": items, "email": email, "shippingAddress": address}
        if offer_code:
            payload["offerCode"] = offer_code
        return self.call("POST", "/agentic/quotes", payload, key)

    def select_shipping(self, quote_id, option_id, key):
        return self.call("POST", f"/agentic/quotes/{quote(quote_id, safe='')}/shipping-option",
                         {"shippingOptionId": option_id}, key)

    def create_checkout(self, payload, key):
        # No simulation-completed header: purchaser must use hosted approval.
        return self.call("POST", "/agentic/checkouts", payload, key)

    def get_checkout(self, checkout_id):
        return self.call("GET", f"/agentic/checkouts/{quote(checkout_id, safe='')}")
