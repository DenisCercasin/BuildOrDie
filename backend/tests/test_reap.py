from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Group, Offer, Order, User
from app.main import create_app
from app.payments import ProviderError, ReapSandbox, money_minor
from conftest import KEY, approve_all, dt, setup_group


class FakeReap:
    def __init__(self):
        self.checkouts = []
        self.user_id = None
        self.total = 7500
        self.status = "REQUIRES_ACTION"
        self.timeout = False

    def get_enrollment(self, _):
        return {"id": "enr-test", "status": "ACTIVE", "owner": {"type": "CLIENT_REFERENCE", "id": self.user_id}}

    def create_quote(self, items, email, address, offer_code, key):
        assert len(items) == 3
        assert address["country"] == "SG"
        return self.get_quote("quote-test")

    def get_quote(self, _):
        return {"id": "quote-test", "expiresAt": dt(minutes=10), "shippingOptions": [],
                "amountBreakdown": {"finalAmount": {"amount": str(self.total / 100), "currency": "SGD"}}}

    def create_checkout(self, payload, key):
        self.checkouts.append((payload, key))
        if self.timeout:
            self.timeout = False
            raise ProviderError("TIMEOUT", uncertain=True)
        return self.get_checkout("co-test")

    def get_checkout(self, _):
        return {"id": "co-test", "status": self.status, "orderId": "merchant-test",
                "finalAmount": {"amount": str(self.total / 100), "currency": "SGD"},
                "nextAction": {"url": "https://example.invalid/hosted-approval"} if self.status == "REQUIRES_ACTION" else None}


def reap_client(tmp_path):
    provider = FakeReap()
    settings = Settings(database_url=f"sqlite:///{tmp_path}/reap.db", service_api_key=KEY,
                        payment_mode="reap_sandbox", reap_api_key="test-not-a-real-key")
    client = TestClient(create_app(settings, provider))
    client.headers["Authorization"] = "Bearer " + KEY
    return client, provider


def prepare(client, provider):
    users, _, offers, _, group = setup_group(client)
    provider.user_id = users[0]["id"]
    with client.app.state.db.transaction(write=True) as s:
        s.get(User, users[0]["id"]).enrollment_id = "enr-test"
        for offer in offers:
            s.get(Offer, offer["id"]).source = "reap"
    response = client.post(f'/v1/groups/{group["id"]}/reap-quote', json={"version": 1, "email": "demo@example.invalid"})
    assert response.status_code == 200, response.text
    group = response.json()["group"]
    approve_all(client, users, group, authorize=True)
    return users, group


def test_reap_hosted_approval_after_simulated_contributions(tmp_path):
    client, provider = reap_client(tmp_path)
    users, group = prepare(client, provider)
    assert group["version"] == 2
    assert all(p["payment_state"] == "AUTHORIZED" for p in client.get(f'/v1/groups/{group["id"]}').json()["participants"])
    order = client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 2}).json()
    assert order["state"] == "AWAITING_PAYMENT_APPROVAL"
    assert order["approval_url"].startswith("https://")
    assert len(provider.checkouts) == 1
    provider.status = "COMPLETED"
    confirmed = client.post(f'/v1/orders/{order["id"]}/reconcile').json()
    assert confirmed["state"] == "ORDERED"
    assert confirmed["merchant_order_id"] == "merchant-test"
    assert len([p for p in client.get("/v1/payments").json() if p["kind"] == "AUTHORIZE"]) == 3
    assert all(p["payment_state"] == "NOT_COLLECTED" for p in client.get(f'/v1/groups/{group["id"]}').json()["participants"])
    assert client.post(f'/v1/orders/{order["id"]}/refund', json={"reason": "Request review"}).json()["state"] == "REFUND_REQUESTED"


def test_reap_ambiguous_request_reuses_persisted_key(tmp_path):
    client, provider = reap_client(tmp_path)
    _, group = prepare(client, provider)
    provider.timeout = True
    order = client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 2}).json()
    assert order["state"] == "PAYMENT_UNKNOWN"
    client.post(f'/v1/orders/{order["id"]}/reconcile')
    assert len(provider.checkouts) == 2
    assert provider.checkouts[0] == provider.checkouts[1]


def test_stale_idempotency_window_never_replays_unknown_charge(tmp_path):
    client, provider = reap_client(tmp_path)
    _, group = prepare(client, provider)
    provider.timeout = True
    order = client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 2}).json()
    with client.app.state.db.transaction(write=True) as s:
        s.get(Order, order["id"]).created_at = dt(days=-2)
    response = client.post(f'/v1/orders/{order["id"]}/reconcile').json()
    assert response["error_code"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert len(provider.checkouts) == 1


def test_reap_final_overcharge_is_flagged_not_silently_allocated(tmp_path):
    client, provider = reap_client(tmp_path)
    _, group = prepare(client, provider)
    order = client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 2}).json()
    provider.status = "COMPLETED"
    provider.total = 9000
    response = client.post(f'/v1/orders/{order["id"]}/reconcile').json()
    assert response["state"] == "RECONCILIATION_REQUIRED"
    assert response["final_minor"] == 9000
    assert all(r["status"] != "ORDERED" for r in client.get("/v1/requests").json())


def test_reap_price_change_blocks_checkout(tmp_path):
    client, provider = reap_client(tmp_path)
    _, group = prepare(client, provider)
    provider.total = 7600
    response = client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 2})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRICE_CHANGED"
    assert not provider.checkouts


def test_enrollment_rejects_return_url_with_different_origin(tmp_path):
    client, provider = reap_client(tmp_path)
    users, _, _, _, _ = setup_group(client)
    for url in ("https://localhost:8000/payment-return", "http://elsewhere.invalid/payment-return"):
        response = client.post(f'/v1/users/{users[0]["id"]}/enrollments',
                               headers={"Idempotency-Key": "enrollment-test"},
                               json={"email": "demo@example.invalid", "return_url": url})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "RETURN_URL_NOT_ALLOWED"


def test_reap_http_contract_uses_sandbox_headers_and_exact_money():
    observed = []
    def handle(request):
        observed.append(request)
        return httpx.Response(200, json={"id": "checkout-test", "status": "REQUIRES_ACTION"})
    settings = Settings(service_api_key=KEY, payment_mode="reap_sandbox", reap_api_key="secret")
    provider = ReapSandbox(settings, httpx.MockTransport(handle))
    provider.create_checkout({"quoteId": "q", "enrollmentId": "e", "presentation": {"type": "REDIRECT"}}, "stable-key")
    request = observed[0]
    assert str(request.url).startswith("https://sg.sandbox.api.reap.global/")
    assert request.headers["Idempotency-Key"] == "stable-key"
    assert request.headers["Reap-Version"] == "2025-02-14"
    assert "X-Simulate-Checkout" not in request.headers
    assert money_minor({"amount": "35.29", "currency": "SGD"}) == 3529
    with pytest.raises(ProviderError):
        money_minor({"amount": "35.291", "currency": "SGD"})
    with pytest.raises(ProviderError):
        money_minor({"amount": "35", "currency": "USD"})


def test_http_enrollment_return_is_rejected_before_provider_call(tmp_path):
    client, _ = reap_client(tmp_path)
    users, _, _, _, _ = setup_group(client)
    response = client.post(f'/v1/users/{users[0]["id"]}/enrollments',
                           headers={"Idempotency-Key": "enrollment-test"},
                           json={"email": "demo@example.com", "return_url": "http://localhost:8000/payment-return"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "REAP_HTTPS_REQUIRED"


def test_https_telegram_enrollment_is_persisted_and_reused(tmp_path):
    class EnrollmentProvider(FakeReap):
        calls = []

        def create_enrollment(self, user_id, email, return_url, key):
            self.calls.append((user_id, email, return_url, key))
            return {"id": "enr-test", "status": "REQUIRES_ACTION",
                    "nextAction": {"url": "https://sandbox.example/card-entry"}}

    provider = EnrollmentProvider()
    settings = Settings(database_url=f"sqlite:///{tmp_path}/enrollment.db", service_api_key=KEY,
                        payment_mode="reap_sandbox", reap_api_key="test",
                        payment_return_url="https://t.me/test_bot")
    client = TestClient(create_app(settings, provider))
    client.headers["Authorization"] = "Bearer " + KEY
    users, _, _, _, _ = setup_group(client)
    user_id = users[0]["id"]
    provider.user_id = user_id
    url = client.get('/health').json()['payment_return_url']
    body = {"email": "demo@example.com", "return_url": url}
    for invalid in ("https://t.me/another_bot", "https://t.me/test_bot?redirect=elsewhere"):
        assert client.post(f'/v1/users/{user_id}/enrollments', headers={"Idempotency-Key": "stable"},
                           json={**body, "return_url": invalid}).status_code == 422
    first = client.post(f'/v1/users/{user_id}/enrollments', headers={"Idempotency-Key": "stable"}, json=body)
    assert first.status_code == 200
    assert first.json()['status'] == 'REQUIRES_ACTION'
    second = client.post(f'/v1/users/{user_id}/enrollments', headers={"Idempotency-Key": "stable"}, json=body)
    assert second.json()['status'] == 'ACTIVE'
    assert len(provider.calls) == 1
    assert provider.calls[0][2] == "https://t.me/test_bot"


def test_production_mode_and_missing_secrets_rejected():
    with pytest.raises(ValueError):
        Settings(service_api_key="").validate()
    with pytest.raises(ValueError):
        Settings(service_api_key=KEY, payment_mode="reap_sandbox").validate()
    with pytest.raises(ValueError):
        Settings(service_api_key=KEY, reap_base_url="https://sg.prod.api.reap.global").validate()
