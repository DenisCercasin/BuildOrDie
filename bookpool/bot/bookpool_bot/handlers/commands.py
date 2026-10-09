from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bookpool_bot.backend import BackendClient
from bookpool_bot.keyboards import INTERRUPT, MAIN_MENU, PICKUP, SETTINGS, request_actions
from bookpool_bot.models import User
from bookpool_bot.presentation import request_line
from bookpool_bot.states import RequestFlow

from .common import ask_next, begin_request, callback_is_private, clear_active_draft, is_private

commands = Router(name="commands")


@commands.message(Command("start"))
async def start(message: Message, state: FSMContext, backend: BackendClient) -> None:
    if not await is_private(message):
        return
    await clear_active_draft(state)
    await backend.upsert_user(User(telegram_user_id=message.from_user.id))
    await message.answer(
        "Welcome to BookPool! 📚\n\nI help you find books and team up with other NTU readers to save on delivery.\n\nWe currently support pickup at NTU in Singapore. Is that where you want to collect your books?",
        reply_markup=PICKUP,
    )


@commands.callback_query(F.data == "m:pickup")
async def pickup(callback: CallbackQuery, backend: BackendClient, state: FSMContext) -> None:
    try:
        if callback.message.chat.type != "private":
            await callback.message.answer("Please use BookPool in a private chat.")
            return
        await backend.upsert_user(
            User(telegram_user_id=callback.from_user.id, pickup_confirmed=True)
        )
        await clear_active_draft(state)
        await callback.message.answer(
            "Great, NTU pickup is set. What would you like to do?", reply_markup=MAIN_MENU
        )
    finally:
        await callback.answer()


@commands.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "BookPool combines book requests from NTU readers to seek lower total prices. Tell me a title, budget, and latest pickup date. Quotes and buy-now advice come from backend services. Your approval never charges you by itself.\n\nCommands: /new, /myrequests, /groups, /settings, /cancel.",
        reply_markup=MAIN_MENU,
    )


@commands.message(Command("cancel"))
async def cancel_flow(message: Message, state: FSMContext) -> None:
    await clear_active_draft(state)
    await message.answer(
        "Current conversation cancelled. Your submitted requests are unchanged.",
        reply_markup=MAIN_MENU,
    )


@commands.message(Command("new"))
async def new_command(message: Message, state: FSMContext, backend: BackendClient) -> None:
    if not await is_private(message):
        return
    current = await state.get_state()
    if current == RequestFlow.interrupt.state:
        await message.answer(
            "Choose what to do with the unfinished request.", reply_markup=INTERRUPT
        )
        return
    if current and current != RequestFlow.interrupt.state:
        await state.update_data(previous_state=current)
        await state.set_state(RequestFlow.interrupt)
        await message.answer(
            "You have an unfinished request. What should I do with it?", reply_markup=INTERRUPT
        )
        return
    await begin_request(message, state, backend, message.from_user.id)


@commands.callback_query(F.data == "m:new")
async def menu_new(callback: CallbackQuery, state: FSMContext, backend: BackendClient) -> None:
    try:
        if not await callback_is_private(callback):
            return
        current = await state.get_state()
        if current == RequestFlow.interrupt.state:
            await callback.message.answer(
                "Choose what to do with the unfinished request.", reply_markup=INTERRUPT
            )
            return
        if current:
            await state.update_data(previous_state=current)
            await state.set_state(RequestFlow.interrupt)
            await callback.message.answer(
                "You have an unfinished request. What should I do with it?", reply_markup=INTERRUPT
            )
        else:
            await begin_request(callback.message, state, backend, callback.from_user.id)
    finally:
        await callback.answer()


@commands.message(Command("resume"))
async def resume(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await message.answer(
            "Finish or cancel your current request before resuming the saved draft."
        )
        return
    data = await state.get_data()
    if not data.get("saved_draft"):
        await message.answer("There is no saved draft. Use /new to start one.")
        return
    draft = data["saved_draft"]
    editing_id = data.get("saved_editing_request_id")
    await state.clear()
    await state.update_data(draft=draft, editing_request_id=editing_id)
    await ask_next(message, state)


@commands.message(Command("settings"))
async def settings(message: Message, backend: BackendClient) -> None:
    if not await is_private(message):
        return
    user = await backend.upsert_user(User(telegram_user_id=message.from_user.id))
    status = "confirmed" if user.pickup_confirmed else "not yet confirmed"
    await message.answer(
        f"Pickup: NTU, Singapore ({status}). Buying preference: {user.preference.replace('_', ' ')}. Choose which matters more to you. Set budget, deadline, format, and savings on each request.",
        reply_markup=SETTINGS,
    )


@commands.callback_query(F.data.startswith("pref:"))
async def preference_choice(callback: CallbackQuery, backend: BackendClient) -> None:
    try:
        if not await callback_is_private(callback):
            return
        preference = callback.data.split(":", 1)[1]
        if preference not in {"lower_cost", "earlier_delivery"}:
            await callback.message.answer("That preference isn't available.")
            return
        existing = await backend.upsert_user(User(telegram_user_id=callback.from_user.id))
        await backend.upsert_user(existing.model_copy(update={"preference": preference}))
        await callback.message.answer(
            "Preference saved. You can still change it for each request.", reply_markup=MAIN_MENU
        )
    finally:
        await callback.answer()


@commands.message(Command("myrequests"))
async def myrequests(message: Message, backend: BackendClient, user_id: int | None = None) -> None:
    if not await is_private(message):
        return
    items = await backend.list_book_requests(user_id or message.from_user.id)
    if not items:
        await message.answer(
            "You have no requests yet. Send a book title or use /new.", reply_markup=MAIN_MENU
        )
    for item in items:
        await message.answer(
            request_line(item),
            reply_markup=request_actions(
                item.request_id,
                getattr(backend, "supports_update", True),
                getattr(backend, "supports_live_search", False),
            ),
        )


@commands.callback_query(F.data == "m:requests")
async def menu_requests(callback: CallbackQuery, backend: BackendClient) -> None:
    try:
        if not await callback_is_private(callback):
            return
        await myrequests(callback.message, backend, callback.from_user.id)
    finally:
        await callback.answer()


@commands.callback_query(F.data == "m:help")
async def menu_help(callback: CallbackQuery) -> None:
    try:
        await help_command(callback.message)
    finally:
        await callback.answer()
