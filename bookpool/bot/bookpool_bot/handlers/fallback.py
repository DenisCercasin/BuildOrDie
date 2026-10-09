from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bookpool_bot.backend import BackendClient
from bookpool_bot.keyboards import INTERRUPT, MAIN_MENU, PICKUP, REVIEW
from bookpool_bot.models import User
from bookpool_bot.parser import BookRequestParser, Intent
from bookpool_bot.states import RequestFlow

from .commands import help_command, myrequests
from .common import clear_active_draft, is_private
from .groups import show_groups
from .requests import book_input

fallback = Router(name="fallback")


@fallback.message(F.text)
async def natural_text(
    message: Message, state: FSMContext, parser: BookRequestParser, backend: BackendClient
) -> None:
    if not await is_private(message):
        return
    current = await state.get_state()
    if current == RequestFlow.review.state:
        await message.answer(
            "Your draft is ready. Use the buttons to search, edit, or cancel.", reply_markup=REVIEW
        )
        return
    if current == RequestFlow.interrupt.state:
        await message.answer(
            "Choose what to do with the unfinished request.", reply_markup=INTERRUPT
        )
        return
    try:
        parsed = await parser.parse(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if parsed.intent == Intent.HELP:
        await help_command(message)
    elif parsed.intent == Intent.VIEW_REQUESTS:
        await myrequests(message, backend)
    elif parsed.intent == Intent.VIEW_GROUPS:
        await show_groups(message, backend)
    elif parsed.title or parsed.isbn:
        user = await backend.upsert_user(User(telegram_user_id=message.from_user.id))
        if not user.pickup_confirmed:
            await message.answer("Please confirm NTU pickup first.", reply_markup=PICKUP)
            return
        await clear_active_draft(state)
        await state.update_data(draft={"preference": user.preference})
        await book_input(message, state, parser)
    else:
        await message.answer(
            "Tell me a book title to begin, or use /help to see what I can do.",
            reply_markup=MAIN_MENU,
        )
