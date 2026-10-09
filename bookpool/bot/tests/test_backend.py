from datetime import timedelta

import httpx
import pytest
import respx
from pydantic import ValidationError

from bookpool_bot.backend import (
    BackendError,
    Conflict,
    HttpBackendClient,
    MockBackendClient,
    NotFound,
)
from bookpool_bot.models import BookRequestInput, Decision, GroupProposal, User
from bookpool_bot.presentation import proposal_text
from bookpool_bot.utils import now, today


@pytest.fixture
def data():
    return BookRequestInput(
        title="Atomic Habits", latest_delivery_date=today() + timedelta(days=10)
    )


@pytest.mark.asyncio
async def test_mock_ownership_repricing_and_idempotency(data):
    backend = MockBackendClient()
    await backend.upsert_user(
        User(telegram_user_id=123, pickup_confirmed=True, preference="earlier_delivery")
    )
    user = await backend.upsert_user(User(telegram_user_id=123))
    assert user.pickup_confirmed and user.preference == "earlier_delivery"
    request = await backend.create_book_request(123, data)
    with pytest.raises(NotFound):
        await backend.get_book_request(456, request.request_id)
    first = await backend.demo_group(123, request.request_id, recommended=True)
    key = f"tg:123:{first.proposal_id}:1:ready"
    result = await backend.submit_group_decision(123, first.proposal_id, 1, Decision.READY, key)
    assert result.recorded
    assert (
        await backend.submit_group_decision(123, first.proposal_id, 1, Decision.READY, key)
        == result
    )
    second = await backend.demo_group(123, request.request_id, revised=True)
    assert second.quote_version == 2
    with pytest.raises(Conflict):
        await backend.submit_group_decision(123, first.proposal_id, 1, Decision.APPROVE, "new-key")
    with pytest.raises(NotFound):
        await backend.get_group_proposal(456, second.proposal_id)
    assert "Buying alone: S$31.90" in proposal_text(second, request.title)
    assert "Buying with group: S$28.20" in proposal_text(second, request.title)


@pytest.mark.asyncio
async def test_expired_proposal(data):
    backend = MockBackendClient()
    request = await backend.create_book_request(123, data)
    proposal = await backend.demo_group(123, request.request_id)
    backend.proposals[proposal.proposal_id] = proposal.model_copy(
        update={"expires_at": now() - timedelta(minutes=1)}
    )
    with pytest.raises(Conflict):
        await backend.submit_group_decision(
            123, proposal.proposal_id, 1, Decision.APPROVE, "expired"
        )


@pytest.mark.asyncio
async def test_quote_invariants(data):
    backend = MockBackendClient()
    request = await backend.create_book_request(123, data)
    proposal = await backend.demo_group(123, request.request_id)
    with pytest.raises(ValidationError, match="savings"):
        GroupProposal.model_validate({**proposal.model_dump(), "savings_minor": 9999})
    with pytest.raises(Conflict, match="Final approval"):
        await backend.submit_group_decision(
            123, proposal.proposal_id, 1, Decision.APPROVE, "premature"
        )


@pytest.mark.asyncio
async def test_http_serialization_and_failures(data):
    backend = HttpBackendClient("https://backend.example", "test-key")
    async with respx.mock:
        route = respx.post("https://backend.example/v1/book-requests").mock(
            return_value=httpx.Response(
                200,
                json={
                    **data.model_dump(mode="json"),
                    "request_id": "r1",
                    "telegram_user_id": 123,
                    "status": "searching",
                    "created_at": now().isoformat(),
                    "updated_at": now().isoformat(),
                },
            )
        )
        result = await backend.create_book_request(123, data)
        assert result.request_id == "r1"
        assert route.calls[0].request.headers["authorization"] == "Bearer test-key"
        assert b'"currency":"SGD"' in route.calls[0].request.content
        respx.get("https://backend.example/v1/users/123/book-requests").mock(
            return_value=httpx.Response(503)
        )
        with pytest.raises(BackendError):
            await backend.list_book_requests(123)
    await backend.close()
