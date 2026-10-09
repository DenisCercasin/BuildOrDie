from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from bookpool_bot.backend import MockBackendClient
from bookpool_bot.handlers import commands, fallback, groups, requests
from bookpool_bot.parser import BookRequestParser


def message_update(number: int, text: str) -> Update:
    return Update.model_validate(
        {
            "update_id": number,
            "message": {
                "message_id": number,
                "date": int(datetime.now().timestamp()),
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "is_bot": False, "first_name": "Alice"},
                "text": text,
            },
        }
    )


def callback_update(number: int, data: str) -> Update:
    return Update.model_validate(
        {
            "update_id": number,
            "callback_query": {
                "id": str(number),
                "from": {"id": 123, "is_bot": False, "first_name": "Alice"},
                "chat_instance": "chat-123",
                "data": data,
                "message": {
                    "message_id": number,
                    "date": int(datetime.now().timestamp()),
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 999, "is_bot": True, "first_name": "BookPool"},
                    "text": "Button message",
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_router_end_to_end_request():
    bot = Bot("123:ABC")
    bot.session.make_request = AsyncMock(return_value=None)
    backend = MockBackendClient()
    dp = Dispatcher(storage=MemoryStorage(), backend=backend, parser=BookRequestParser())
    dp.include_routers(commands, requests, groups, fallback)
    try:
        await dp.feed_update(bot, message_update(1, "/start"))
        await dp.feed_update(bot, callback_update(2, "m:pickup"))
        await dp.feed_update(
            bot,
            message_update(
                3, "I want Atomic Habits, paperback, under S$35, and I can wait 10 days"
            ),
        )
        await dp.feed_update(bot, callback_update(4, "s:skip"))
        await dp.feed_update(bot, callback_update(5, "r:confirm"))
        items = await backend.list_book_requests(123)
        assert len(items) == 1
        assert items[0].title == "Atomic Habits"
        assert items[0].maximum_budget_minor == 3500
        assert bot.session.make_request.await_count >= 5
    finally:
        await bot.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("purchaser", [True, False])
async def test_checkout_blocked_explains_pending_card_without_payment_link(purchaser):
    from bookpool_bot.models import NotificationEvent
    from bookpool_bot.notifications import NotificationDispatcher

    bot = AsyncMock()
    backend = AsyncMock()
    dispatcher = NotificationDispatcher(bot, backend)
    event = NotificationEvent(
        event_id="blocked:123",
        event_type="checkout_blocked",
        telegram_user_id=123,
        request_id="r1",
        proposal_id="g1",
        occurred_at=datetime.now(),
        payload={"is_purchaser": purchaser},
    )
    assert await dispatcher.deliver(event)
    text = bot.send_message.call_args.args[1]
    assert "No checkout or payment has been created" in text
    assert ("keep that page open" in text) == purchaser
    assert "https://" not in text
    assert await dispatcher.deliver(event)
    assert bot.send_message.await_count == 1
