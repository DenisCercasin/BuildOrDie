import re

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from bookpool_bot.backend import BackendClient
from bookpool_bot.keyboards import (
    DEADLINE,
    FORMAT,
    INTERRUPT,
    PICKUP,
    REVIEW,
    SKIP_BUDGET,
    SKIP_SAVINGS,
)
from bookpool_bot.models import BookRequestInput, User
from bookpool_bot.parser import BookRequestParser
from bookpool_bot.presentation import request_summary
from bookpool_bot.states import RequestFlow


async def clear_active_draft(state: FSMContext) -> dict:
    data = await state.get_data()
    saved = {key: data[key] for key in ("saved_draft", "saved_editing_request_id") if key in data}
    await state.clear()
    if saved:
        await state.set_data(saved)
    return saved


async def is_private(message: Message) -> bool:
    if message.chat.type != "private":
        await message.answer("Please message BookPool privately to manage your requests.")
        return False
    return True


async def callback_is_private(callback: CallbackQuery) -> bool:
    if callback.message.chat.type != "private":
        await callback.message.answer("Please message BookPool privately to manage your requests.")
        return False
    return True


async def maybe_new_book(message: Message, state: FSMContext, parser: BookRequestParser) -> bool:
    if re.match(r"^i (?:need it|want to wait|need to wait)\b", message.text, re.I):
        return False
    if not re.match(r"^(?:i (?:want|need)|find(?: me)?|looking for)\s+", message.text, re.I):
        return False
    parsed = await parser.parse(message.text)
    if not parsed.title and not parsed.isbn:
        return False
    current = await state.get_state()
    await state.update_data(previous_state=current)
    await state.set_state(RequestFlow.interrupt)
    await message.answer(
        "That sounds like a new book. Finish, discard, or save your current draft first. If you start a new one, send the title again.",
        reply_markup=INTERRUPT,
    )
    return True


async def ask_next(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft", {})
    if not draft.get("title") and not draft.get("isbn"):
        await state.set_state(RequestFlow.book)
        await message.answer(
            "What book are you looking for? Send a title or ISBN. /cancel returns to the menu."
        )
    elif not draft.get("format"):
        await state.set_state(RequestFlow.format)
        await message.answer("Any format preference?", reply_markup=FORMAT)
    elif not draft.get("latest_delivery_date"):
        await state.set_state(RequestFlow.deadline)
        await message.answer(
            "What's the latest date you can pick it up at NTU?", reply_markup=DEADLINE
        )
    elif "maximum_budget_minor" not in draft:
        await state.set_state(RequestFlow.budget)
        await message.answer(
            "What's your maximum total budget, including delivery?", reply_markup=SKIP_BUDGET
        )
    elif "minimum_savings_minor" not in draft and "minimum_savings_percent" not in draft:
        await state.set_state(RequestFlow.savings)
        await message.answer(
            "What minimum savings make waiting worthwhile?", reply_markup=SKIP_SAVINGS
        )
    else:
        await show_review(message, state)


async def show_review(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = {**data["draft"]}
    draft["maximum_budget_minor"] = draft.get("maximum_budget_minor")
    draft["minimum_savings_minor"] = draft.get("minimum_savings_minor", 0)
    try:
        request = BookRequestInput.model_validate(draft)
    except ValidationError:
        await message.answer("Some request details need correction. Let's check the book first.")
        await state.update_data(draft={})
        await ask_next(message, state)
        return
    await state.set_state(RequestFlow.review)
    await message.answer(request_summary(request), reply_markup=REVIEW)


async def begin_request(
    message: Message, state: FSMContext, backend: BackendClient, user_id: int
) -> None:
    user = await backend.upsert_user(User(telegram_user_id=user_id))
    if not user.pickup_confirmed:
        await message.answer("Please confirm NTU pickup first.", reply_markup=PICKUP)
        return
    await clear_active_draft(state)
    await state.update_data(draft={"preference": user.preference})
    await ask_next(message, state)
