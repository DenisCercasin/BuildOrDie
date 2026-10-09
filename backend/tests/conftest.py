from datetime import datetime, timedelta, timezone
import uuid
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

KEY = "test-service-key-01234567890123456789"


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/test.db", service_api_key=KEY))
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + KEY
        yield client


def dt(days=0, minutes=0):
    return (datetime.now(timezone.utc) + timedelta(days=days, minutes=minutes)).isoformat()


def setup_group(client, count=3, reference=None):
    users, reqs, offers = [], [], []
    expiry, delivery, deadline = dt(minutes=30), dt(days=3), dt(days=7)
    for i in range(count):
        user = client.post("/v1/users", json={"telegram_id": uuid.uuid4().hex,
                                            "display_name": ["Alice", "Bob", "Charlie"][i % 3]}).json()
        users.append(user)
        request = client.post("/v1/requests", json={
            "user_id": user["id"], "title": f"Different book {i}", "budget_minor": 3500,
            "min_savings_minor": 100, "latest_delivery_at": deadline, "pickup_location": "NTU"})
        assert request.status_code == 200, request.text
        reqs.append(request.json())
        offer = client.post("/v1/offers", json={
            "request_id": reqs[-1]["id"], "merchant": "kinokuniya.com.sg", "variant_id": f"mock-variant-{i}",
            "unit_price_minor": 2500, "individual_total_minor": 3100,
            "delivery_at": delivery, "expires_at": expiry})
        assert offer.status_code == 200, offer.text
        offers.append(offer.json())
    proposal = {"client_reference": reference or uuid.uuid4().hex, "merchant": "kinokuniya.com.sg",
                "purchaser_id": users[0]["id"], "total_minor": 2500 * count,
                "expires_at": expiry, "delivery_at": delivery, "pickup_location": "NTU",
                "reason": "Illustrative mock prices: combined cart avoids separate delivery fees.",
                "shipping_address": {"firstName": "Demo", "lastName": "Buyer", "phone": "+6500000000",
                                     "addressLine1": "Test address — do not ship", "postalCode": "000000"},
                "allocations": [{"request_id": r["id"], "offer_id": o["id"], "amount_minor": 2500}
                                for r, o in zip(reqs, offers)]}
    response = client.post("/v1/groups", json=proposal)
    assert response.status_code == 200, response.text
    return users, reqs, offers, proposal, response.json()


def approve_all(client, users, group, authorize=True):
    for u in users:
        path = f'/v1/groups/{group["id"]}/participants/{u["id"]}'
        response = client.post(path + "/approve", json={"version": group["version"]})
        assert response.status_code == 200, response.text
        if authorize:
            response = client.post(path + "/authorize", json={"version": group["version"]})
            assert response.status_code == 200, response.text
