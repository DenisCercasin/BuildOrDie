"""Full four-user group flow through live-price adapter, engine and backend."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings
from app.main import create_app
from bookpool_bot.merchant_search import CatalogMatch, SearchResult
from bookpool_bot.models import BookRequestInput, Decision, User
from bookpool_bot.notifications import NotificationDispatcher
from bookpool_bot.team_backend import TeamBackendClient

from bookpool.orchestrator.reap_catalog import title_matches
from bookpool.orchestrator.worker import GroupWorker

KEY = "test-service-key-01234567890123456789"


def test_reap_title_match_rejects_related_books():
    assert title_matches(
        "Atomic Habits", "Atomic Habits: Tiny Changes, Remarkable Results"
    )
    assert not title_matches(
        "Atomic Habits", "Designing Your Life: For Fans of Atomic Habits"
    )


class Catalog:
    async def search(self, request):
        slug = request.title.lower().replace(" ", "-")
        return SearchResult(
            [
                CatalogMatch(
                    "Kinokuniya",
                    request.title,
                    3000,
                    f"https://kinokuniya.com.sg/products/{slug}",
                ),
                CatalogMatch(
                    "POPULAR",
                    request.title,
                    3000,
                    f"https://popular.com.sg/products/{slug}",
                ),
            ],
            [],
        )


def test_four_users_approve_and_authorize_simulated_order(tmp_path):
    asyncio.run(run_flow(tmp_path))


def test_impossible_deadline_notifies_owner_once(tmp_path):
    asyncio.run(run_deadline_feedback(tmp_path))


async def run_deadline_feedback(tmp_path):
    app = create_app(
        Settings(database_url=f"sqlite:///{tmp_path}/deadline.db", service_api_key=KEY)
    )
    transport = httpx.ASGITransport(app=app)
    client = TeamBackendClient("http://test", KEY, tmp_path / "deadline_state.json")
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    worker = GroupWorker("http://test", KEY, catalog=Catalog(), client=client.client)

    class Bot:
        def __init__(self):
            self.messages = []

        async def send_message(self, user_id, text, **kwargs):
            self.messages.append((user_id, text))

    try:
        for telegram_id in [100, 101]:
            await client.upsert_user(User(telegram_user_id=telegram_id))
        request = await client.create_book_request(
            100,
            BookRequestInput(
                title="Educated",
                maximum_budget_minor=5000,
                latest_delivery_date=datetime.now(timezone.utc).date()
                + timedelta(days=1),
            ),
        )
        await worker.run_once()
        assert await worker.api("GET", "/v1/groups") == []
        events = await client.fetch_pending_events()
        assert len(events) == 1
        assert events[0].telegram_user_id == 100
        assert events[0].request_id == request.request_id
        assert events[0].payload["code"] == "DEADLINE_TOO_SOON"
        repeated = await worker.api(
            "POST",
            f"/v1/requests/{request.request_id}/search-status",
            json={
                key: events[0].payload[key] for key in ["code", "earliest_delivery_at"]
            },
        )
        assert repeated == {"changed": False}
        bot = Bot()
        dispatcher = NotificationDispatcher(bot, client)
        assert await dispatcher.deliver(events[0])
        assert len(bot.messages) == 1
        assert "after your" in bot.messages[0][1]
        assert "delivery estimate" in bot.messages[0][1]
        assert "/myrequests" in bot.messages[0][1]
        assert await client.fetch_pending_events() == []
    finally:
        await client.close()


async def run_flow(tmp_path):
    app = create_app(
        Settings(database_url=f"sqlite:///{tmp_path}/flow.db", service_api_key=KEY)
    )
    transport = httpx.ASGITransport(app=app)
    client = TeamBackendClient("http://test", KEY, tmp_path / "bot_state.json")
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    worker_http = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    worker = GroupWorker("http://test", KEY, catalog=Catalog(), client=worker_http)
    try:
        delivery_deadline = datetime.now(timezone.utc).date() + timedelta(days=30)
        for telegram_id, title in enumerate(
            ["Atomic Habits", "Deep Work", "The Psychology of Money", "Educated"],
            start=100,
        ):
            await client.upsert_user(
                User(telegram_user_id=telegram_id, pickup_confirmed=True)
            )
            await client.create_book_request(
                telegram_id,
                BookRequestInput(
                    title=title,
                    maximum_budget_minor=4000,
                    latest_delivery_date=delivery_deadline,
                ),
            )
        await worker.run_once()
        groups = await worker.api("GET", "/v1/groups")
        assert len(groups) == 1
        group = groups[0]
        assert len(group["participants"]) == 4
        assert group["merchant"] in {"kinokuniya.com.sg", "popular.com.sg"}
        assert group["total_minor"] < 4 * 3480
        for telegram_id in range(100, 104):
            proposal = await client.get_group_proposal(telegram_id, group["id"])
            assert proposal.participant_count == 4
            assert not proposal.user_approved
            approved = await client.submit_group_decision(
                telegram_id,
                group["id"],
                group["version"],
                Decision.APPROVE,
                f"approve-{telegram_id}",
            )
            assert approved.proposal.user_approved
            contributed = await client.submit_group_decision(
                telegram_id,
                group["id"],
                group["version"],
                Decision.CONTRIBUTE,
                f"contribute-{telegram_id}",
            )
            assert contributed.proposal.payment_status == "AUTHORIZED"
        ready = await worker.api("GET", f"/v1/groups/{group['id']}")
        assert ready["state"] == "READY"
        await worker.run_once()
        ordered = await worker.api("GET", f"/v1/groups/{group['id']}")
        assert ordered["state"] == "ORDERED"
        assert ordered["order"]["provider"] == "mock"
        assert ordered["order"]["simulated"]
    finally:
        await client.close()
        await worker_http.aclose()
