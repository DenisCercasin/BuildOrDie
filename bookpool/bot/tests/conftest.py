from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


class FakeMessage:
    def __init__(self, user_id: int = 123, text: str = ""):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type="private")
        self.text = text
        self.sent: list[tuple[str, object]] = []

    async def answer(self, text: str, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))


class FakeCallback:
    def __init__(self, data: str, user_id: int = 123):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage(user_id=0)
        self.answered = False

    async def answer(self):
        self.answered = True


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=123, user_id=123))
