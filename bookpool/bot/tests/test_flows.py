from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from conftest import FakeCallback, FakeMessage

from bookpool_bot.backend import MockBackendClient
from bookpool_bot.handlers import (
    begin_request,
    book_input,
    cancel_flow,
    demo,
    format_text,
    interrupt_choice,
    new_command,
    proposal_action,
    resume,
    review_choice,
    skip_savings,
)
from bookpool_bot.models import BookRequestInput, RequestStatus, User
from bookpool_bot.notifications import NotificationDispatcher
from bookpool_bot.parser import BookRequestParser
from bookpool_bot.states import RequestFlow
from bookpool_bot.utils import today


@pytest.mark.asyncio
async def test_request_confirmation_and_cancel(state):
    backend = MockBackendClient()
    await backend.upsert_user(User(telegram_user_id=123, pickup_confirmed=True))
    message = FakeMessage()
    await begin_request(message, state, backend, 123)
    assert await state.get_state() == RequestFlow.book.state
    message.text = "I want Atomic Habits, paperback, under S$35, and I can wait 10 days"
    await book_input(message, state, BookRequestParser())
    assert await state.get_state() == RequestFlow.savings.state
    callback = FakeCallback("s:skip")
    await skip_savings(callback, state)
    assert callback.answered
    assert await state.get_state() == RequestFlow.review.state
    assert "Prices and availability have not been checked" in callback.message.sent[-1][0]
    callback = FakeCallback("r:confirm")
    await review_choice(callback, state, backend)
    assert callback.answered
    assert len(await backend.list_book_requests(123)) == 1
    assert await state.get_state() is None
    await begin_request(message, state, backend, 123)
    await cancel_flow(message, state)
    assert await state.get_state() is None
    assert len(await backend.list_book_requests(123)) == 1


@pytest.mark.asyncio
async def test_new_title_interrupts_draft(state):
    backend = MockBackendClient()
    await backend.upsert_user(User(telegram_user_id=123, pickup_confirmed=True))
    message = FakeMessage()
    await begin_request(message, state, backend, 123)
    message.text = "Atomic Habits"
    await book_input(message, state, BookRequestParser())
    assert await state.get_state() == RequestFlow.format.state
    message.text = "I want Deep Work"
    await format_text(message, state, BookRequestParser())
    assert await state.get_state() == RequestFlow.interrupt.state
    assert "new book" in message.sent[-1][0]


@pytest.mark.asyncio
async def test_saved_draft_survives_another_request(state):
    backend = MockBackendClient()
    await backend.upsert_user(User(telegram_user_id=123, pickup_confirmed=True))
    message = FakeMessage(text="Atomic Habits")
    await begin_request(message, state, backend, 123)
    await book_input(message, state, BookRequestParser())
    message.text = "/new"
    await new_command(message, state, backend)
    assert await state.get_state() == RequestFlow.interrupt.state
    await interrupt_choice(FakeCallback("i:save"), state, backend)
    assert await state.get_state() is None
    await begin_request(message, state, backend, 123)
    await cancel_flow(message, state)
    await resume(message, state)
    assert await state.get_state() == RequestFlow.format.state
    assert (await state.get_data())["draft"]["title"] == "Atomic Habits"


@pytest.mark.asyncio
async def test_unauthorized_and_duplicate_proposal_callback():
    backend = MockBackendClient()
    request = await backend.create_book_request(
        123,
        __import__("bookpool_bot.models", fromlist=["BookRequestInput"]).BookRequestInput(
            title="Deep Work", latest_delivery_date=today() + timedelta(days=7)
        ),
    )
    proposal = await backend.demo_group(123, request.request_id)
    unauth = FakeCallback(f"p:ready:{proposal.proposal_id}:1", user_id=456)
    await proposal_action(unauth, backend)
    assert unauth.answered
    assert "available to you" in unauth.message.sent[-1][0]
    callback = FakeCallback(f"p:ready:{proposal.proposal_id}:1")
    await proposal_action(callback, backend)
    await proposal_action(callback, backend)
    assert len(backend.decisions) == 1


@pytest.mark.asyncio
async def test_notification_ack_and_duplicate():
    backend = MockBackendClient()
    request = await backend.create_book_request(
        123,
        __import__("bookpool_bot.models", fromlist=["BookRequestInput"]).BookRequestInput(
            title="Deep Work", latest_delivery_date=today() + timedelta(days=7)
        ),
    )
    proposal = await backend.demo_group(123, request.request_id, recommended=True)
    event = await backend.demo_event(
        "buy_now_recommended", 123, request.request_id, proposal.proposal_id
    )
    bot = AsyncMock()
    dispatcher = NotificationDispatcher(bot, backend)
    assert await dispatcher.deliver(event)
    assert await dispatcher.deliver(event)
    bot.send_message.assert_awaited_once()
    assert not await backend.fetch_pending_events()


@pytest.mark.asyncio
async def test_complete_fictional_demo_sequence():
    backend = MockBackendClient()
    request = await backend.create_book_request(
        123,
        BookRequestInput(title="Atomic Habits", latest_delivery_date=today() + timedelta(days=10)),
    )
    bot = AsyncMock()
    dispatcher = NotificationDispatcher(bot, backend)
    message = FakeMessage(text="/demo recommend")
    await demo(message, backend)
    await dispatcher.run_once()
    old_proposal = (await backend.list_user_group_proposals(123))[0]
    await proposal_action(FakeCallback(f"p:ready:{old_proposal.proposal_id}:1"), backend)
    assert len(backend.decisions) == 1

    message.text = "/demo approval"
    await demo(message, backend)
    await dispatcher.run_once()
    final = (await backend.list_user_group_proposals(123))[0]
    assert final.quote_version == 2
    await proposal_action(FakeCallback(f"p:approve:{final.proposal_id}:2"), backend)
    assert len(backend.decisions) == 2

    message.text = "/demo reprice"
    await demo(message, backend)
    await dispatcher.run_once()
    stale = FakeCallback(f"p:approve:{final.proposal_id}:2")
    await proposal_action(stale, backend)
    assert "expired or changed" in stale.message.sent[-1][0]
    assert len(backend.decisions) == 2

    message.text = "/demo complete"
    await demo(message, backend)
    await dispatcher.run_once()
    assert (
        await backend.get_book_request(123, request.request_id)
    ).status == RequestStatus.ORDER_PLACED
    assert bot.send_message.await_count == 4
