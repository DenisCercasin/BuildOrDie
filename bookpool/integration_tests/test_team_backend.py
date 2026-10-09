"""Run with backend/.venv/bin/python -m pytest bookpool/integration_tests -q from repo root."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings
from app.main import create_app
from bookpool_bot.backend import Conflict
from bookpool_bot.models import BookRequestInput, Decision, User
from bookpool_bot.team_backend import TeamBackendClient

KEY = "test-service-key-01234567890123456789"


def stamp(*, days: int = 0, hours: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=hours)).isoformat()


def test_bot_adapter_with_actual_backend(tmp_path):
    asyncio.run(run_scenario(tmp_path))


async def run_scenario(tmp_path):
    app = create_app(
        Settings(database_url=f"sqlite:///{tmp_path}/bookpool.db", service_api_key=KEY)
    )
    transport = httpx.ASGITransport(app=app)
    client = TeamBackendClient("http://test", KEY, tmp_path / "bot_state.json")
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    admin = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    try:
        users, requests, offers = [], [], []
        for telegram_id, title in [(123, "Atomic Habits"), (456, "Deep Work")]:
            await client.upsert_user(
                User(telegram_user_id=telegram_id, pickup_confirmed=True)
            )
            users.append(client.user_ids[telegram_id])
            request = await client.create_book_request(
                telegram_id,
                BookRequestInput(
                    title=title,
                    latest_delivery_date=datetime.now(timezone.utc).date()
                    + timedelta(days=7),
                    maximum_budget_minor=3500,
                ),
            )
            requests.append(request)
            assert (
                await client.get_book_request(telegram_id, request.request_id)
            ).title == title
            response = await admin.post(
                "/v1/offers",
                json={
                    "request_id": request.request_id,
                    "merchant": "kinokuniya.com.sg",
                    "variant_id": f"demo-{telegram_id}",
                    "unit_price_minor": 2500,
                    "individual_total_minor": 3100,
                    "delivery_at": stamp(days=3),
                    "expires_at": stamp(hours=2),
                    "source": "mock",
                },
            )
            assert response.status_code == 200, response.text
            offers.append(response.json())

        proposal_payload = {
            "client_reference": "cross-component-test-1",
            "merchant": "kinokuniya.com.sg",
            "purchaser_id": users[0],
            "total_minor": 5000,
            "currency": "SGD",
            "expires_at": stamp(hours=1),
            "delivery_at": stamp(days=3),
            "pickup_location": "NTU shared collection point",
            "reason": "Fictional group quote for integration testing.",
            "allocations": [
                {
                    "request_id": request.request_id,
                    "offer_id": offer["id"],
                    "amount_minor": 2500,
                }
                for request, offer in zip(requests, offers)
            ],
        }
        response = await admin.post("/v1/groups", json=proposal_payload)
        assert response.status_code == 200, response.text
        group = response.json()
        shown = (await client.list_user_group_proposals(123))[0]
        assert shown.proposal_id == group["id"]
        assert shown.group_total_minor == 2500
        assert shown.individual_total_minor == 3100
        assert shown.item_price_minor is None
        assert len(await client.list_user_group_proposals(789)) == 0

        decision = await client.submit_group_decision(
            123, group["id"], 1, Decision.APPROVE, "telegram:123:group:1:approve"
        )
        assert decision.recorded
        assert "No payment" in decision.message
        assert (await client.get_group_proposal(123, group["id"])).user_approved
        response = await admin.put(
            f"/v1/groups/{group['id']}/proposal",
            params={"expected_version": 1},
            json={
                **proposal_payload,
                "total_minor": 5200,
                "allocations": [
                    {**allocation, "amount_minor": 2600}
                    for allocation in proposal_payload["allocations"]
                ],
            },
        )
        assert response.status_code == 200, response.text
        with pytest.raises(Conflict, match="changed"):
            await client.submit_group_decision(
                123, group["id"], 1, Decision.APPROVE, "telegram:123:group:1:approve"
            )
        assert (await client.get_group_proposal(123, group["id"])).quote_version == 2

        events = await client.fetch_pending_events()
        assert {event.telegram_user_id for event in events} == {123, 456}
        for event in events:
            await client.acknowledge_event(event.event_id)
        assert client.cursor > 0
        assert (
            TeamBackendClient("http://test", KEY, tmp_path / "bot_state.json").cursor
            == client.cursor
        )
    finally:
        await client.close()
        await admin.aclose()
