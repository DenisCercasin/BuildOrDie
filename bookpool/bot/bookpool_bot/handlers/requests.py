from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from bookpool_bot.backend import BackendClient, BackendError, Conflict
from bookpool_bot.keyboards import (
    DEADLINE,
    EDIT,
    FORMAT,
    MAIN_MENU,
    REVIEW,
    SKIP_BUDGET,
    SKIP_SAVINGS,
    request_actions,
)
from bookpool_bot.merchant_search import MerchantSearchClient
from bookpool_bot.models import BookFormat, BookRequestInput
from bookpool_bot.parser import BookRequestParser
from bookpool_bot.presentation import request_line
from bookpool_bot.states import RequestFlow
from bookpool_bot.utils import money, parse_amount_minor, parse_deadline, today

from .common import (
    ask_next,
    begin_request,
    callback_is_private,
    clear_active_draft,
    maybe_new_book,
    show_review,
)

requests = Router(name="requests")


@requests.callback_query(RequestFlow.interrupt, F.data.startswith("i:"))
async def interrupt_choice(
    callback: CallbackQuery, state: FSMContext, backend: BackendClient
) -> None:
    try:
        choice = callback.data.split(":")[1]
        data = await state.get_data()
        if choice == "finish":
            await state.set_state(data.get("previous_state", RequestFlow.book.state))
            await callback.message.answer("Continue your request. /cancel returns to the menu.")
        elif choice == "save":
            await state.clear()
            saved = {"saved_draft": data.get("draft", {})}
            if data.get("editing_request_id"):
                saved["saved_editing_request_id"] = data["editing_request_id"]
            await state.set_data(saved)
            await callback.message.answer(
                "Draft saved in this chat session. Use /resume to continue it. Drafts are lost if the bot restarts. One saved draft can be held at a time.",
                reply_markup=MAIN_MENU,
            )
        elif choice == "discard":
            await begin_request(callback.message, state, backend, callback.from_user.id)
    finally:
        await callback.answer()


@requests.message(RequestFlow.book, F.text)
async def book_input(message: Message, state: FSMContext, parser: BookRequestParser) -> None:
    try:
        parsed = await parser.parse(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if not parsed.title and not parsed.isbn:
        await message.answer("I need a book title or valid ISBN to continue.")
        return
    data = await state.get_data()
    draft = {**data.get("draft", {})}
    for key in (
        "title",
        "isbn",
        "author",
        "edition",
        "language",
        "format",
        "maximum_budget_minor",
        "latest_delivery_date",
        "minimum_savings_minor",
        "minimum_savings_percent",
    ):
        value = getattr(parsed, key)
        if value is not None:
            draft[key] = (
                value.value
                if isinstance(value, BookFormat)
                else value.isoformat()
                if hasattr(value, "isoformat")
                else value
            )
    await state.update_data(draft=draft)
    await ask_next(message, state)


@requests.callback_query(RequestFlow.format, F.data.startswith("f:"))
async def format_choice(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        choice = callback.data.split(":")[1]
        if choice not in {"paperback", "hardcover", "any"}:
            return
        data = await state.get_data()
        await state.update_data(draft={**data["draft"], "format": choice})
        await ask_next(callback.message, state)
    finally:
        await callback.answer()


@requests.message(RequestFlow.format, F.text)
async def format_text(message: Message, state: FSMContext, parser: BookRequestParser) -> None:
    if await maybe_new_book(message, state, parser):
        return
    lower = message.text.lower().strip()
    choice = (
        "paperback"
        if "paperback" in lower
        else "hardcover"
        if "hardcover" in lower or "hardback" in lower
        else "any"
        if lower in {"any", "no preference"}
        else None
    )
    if choice is None:
        await message.answer(
            "Please choose paperback, hardcover, or no preference.", reply_markup=FORMAT
        )
        return
    data = await state.get_data()
    await state.update_data(draft={**data["draft"], "format": choice})
    await ask_next(message, state)


@requests.callback_query(RequestFlow.deadline, F.data.startswith("d:"))
async def deadline_choice(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        choice = callback.data.split(":")[1]
        if choice == "custom":
            await callback.message.answer(
                "Send a date like 2026-10-25 or 'I can wait 10 days'. This means the latest NTU pickup date."
            )
            return
        if choice not in {"3", "7", "14"}:
            return
        data = await state.get_data()
        await state.update_data(
            draft={
                **data["draft"],
                "latest_delivery_date": (today() + timedelta(days=int(choice))).isoformat(),
            }
        )
        await ask_next(callback.message, state)
    finally:
        await callback.answer()


@requests.message(RequestFlow.deadline, F.text)
async def deadline_text(message: Message, state: FSMContext, parser: BookRequestParser) -> None:
    if await maybe_new_book(message, state, parser):
        return
    try:
        deadline = parse_deadline(message.text)
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=DEADLINE)
        return
    data = await state.get_data()
    await state.update_data(draft={**data["draft"], "latest_delivery_date": deadline.isoformat()})
    await ask_next(message, state)


@requests.callback_query(RequestFlow.budget, F.data == "b:skip")
async def skip_budget(callback: CallbackQuery, state: FSMContext, backend: BackendClient) -> None:
    try:
        if getattr(backend, "requires_budget", False):
            await callback.message.answer(
                "The connected backend requires a maximum total budget. Please enter an amount such as S$35."
            )
            return
        data = await state.get_data()
        await state.update_data(draft={**data["draft"], "maximum_budget_minor": None})
        await ask_next(callback.message, state)
    finally:
        await callback.answer()


@requests.message(RequestFlow.budget, F.text)
async def budget_text(message: Message, state: FSMContext, parser: BookRequestParser) -> None:
    if await maybe_new_book(message, state, parser):
        return
    try:
        amount = parse_amount_minor(message.text)
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=SKIP_BUDGET)
        return
    data = await state.get_data()
    await state.update_data(draft={**data["draft"], "maximum_budget_minor": amount})
    await ask_next(message, state)


@requests.callback_query(RequestFlow.savings, F.data == "s:skip")
async def skip_savings(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        data = await state.get_data()
        draft = {**data["draft"], "minimum_savings_minor": 0}
        draft.pop("minimum_savings_percent", None)
        await state.update_data(draft=draft)
        await ask_next(callback.message, state)
    finally:
        await callback.answer()


@requests.message(RequestFlow.savings, F.text)
async def savings_text(message: Message, state: FSMContext, parser: BookRequestParser) -> None:
    if await maybe_new_book(message, state, parser):
        return
    if "%" in message.text:
        import re
        from decimal import Decimal, InvalidOperation

        match = re.search(r"(\d+(?:\.\d{1,2})?)\s*%", message.text)
        try:
            percentage = Decimal(match.group(1)) if match else None
        except InvalidOperation:
            percentage = None
        if percentage is None or not 0 <= percentage <= 100:
            await message.answer("Enter a percentage from 0% to 100%, such as 10%.")
            return
        data = await state.get_data()
        await state.update_data(
            draft={
                **data["draft"],
                "minimum_savings_minor": 0,
                "minimum_savings_percent": str(percentage),
            }
        )
        await ask_next(message, state)
        return
    amount = 0 if "any" in message.text.lower() else None
    if amount is None:
        try:
            amount = parse_amount_minor(message.text)
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=SKIP_SAVINGS)
            return
    data = await state.get_data()
    draft = {**data["draft"], "minimum_savings_minor": amount}
    draft.pop("minimum_savings_percent", None)
    await state.update_data(draft=draft)
    await ask_next(message, state)


@requests.callback_query(RequestFlow.review, F.data.startswith("r:"))
async def review_choice(
    callback: CallbackQuery,
    state: FSMContext,
    backend: BackendClient,
    merchant_search: MerchantSearchClient | None = None,
) -> None:
    answered = False
    try:
        choice = callback.data.split(":")[1]
        if choice == "cancel":
            await clear_active_draft(state)
            await callback.message.answer("Draft discarded.", reply_markup=MAIN_MENU)
        elif choice == "edit":
            await callback.message.answer("What would you like to change?", reply_markup=EDIT)
        elif choice == "confirm":
            data = await state.get_data()
            request = BookRequestInput.model_validate(data["draft"])
            if request.latest_delivery_date < today():
                await callback.message.answer(
                    "Your latest pickup date has passed. Please edit the deadline.",
                    reply_markup=EDIT,
                )
                return
            if data.get("editing_request_id"):
                saved = await backend.update_book_request(
                    callback.from_user.id, data["editing_request_id"], request
                )
                message = "Request updated. Previous quotes must be refreshed."
            else:
                saved = await backend.create_book_request(callback.from_user.id, request)
                message = "Request registered. We'll look for offers; no price or availability has been verified yet."
            await clear_active_draft(state)
            await callback.message.answer(
                f"{message}\n\n{request_line(saved)}",
                reply_markup=request_actions(
                    saved.request_id,
                    getattr(backend, "supports_update", True),
                    getattr(backend, "supports_live_search", False),
                ),
            )
            if getattr(backend, "supports_live_search", False):
                await callback.answer()
                answered = True
                await send_live_search(callback.message, saved, merchant_search)
    except (BackendError, ValidationError) as exc:
        await callback.message.answer(
            str(exc)
            if isinstance(exc, Conflict)
            else "Could not save your request. Please try again shortly."
        )
    finally:
        if not answered:
            await callback.answer()


async def send_live_search(
    message: Message, request: BookRequestInput, merchant_search: MerchantSearchClient | None
) -> None:
    if merchant_search is None:
        await message.answer("Live store search is unavailable right now. Try again shortly.")
        return
    result = await merchant_search.search(request)
    if not result.matches:
        detail = (
            "Both store searches are temporarily unavailable."
            if len(result.failed_stores) == 2
            else "No matching in-stock listings were found at the approved stores."
        )
        await message.answer(f"{detail} Use Check store prices later to retry.")
        return
    lines = ["Live bookstore listings (book price only):", ""]
    for match in result.matches:
        lines.append(f"{match.merchant} — {match.title}\n{money(match.price_minor)} · {match.url}")
    lines.extend(
        [
            "",
            "Delivery charges and NTU arrival dates are not verified yet. These are listings, not group offers or final prices.",
        ]
    )
    if result.failed_stores:
        lines.append(f"Could not check: {', '.join(result.failed_stores)}.")
    await message.answer("\n\n".join(lines))


@requests.callback_query(RequestFlow.review, F.data.startswith("e:"))
async def edit_choice(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        choice = callback.data.split(":")[1]
        if choice == "back":
            await show_review(callback.message, state)
            return
        field, target, prompt = {
            "book": ("title", RequestFlow.book, "Send the new book title or ISBN."),
            "format": ("format", RequestFlow.format, "Choose a new format."),
            "deadline": (
                "latest_delivery_date",
                RequestFlow.deadline,
                "Send a new latest pickup date.",
            ),
            "budget": (
                "maximum_budget_minor",
                RequestFlow.budget,
                "Send your new maximum total budget.",
            ),
            "savings": (
                "minimum_savings_minor",
                RequestFlow.savings,
                "Send your new minimum savings.",
            ),
        }[choice]
        data = await state.get_data()
        draft = {**data["draft"]}
        draft.pop(field, None)
        if choice == "book":
            draft.pop("isbn", None)
        elif choice == "savings":
            draft.pop("minimum_savings_percent", None)
        await state.update_data(draft=draft)
        await state.set_state(target)
        await callback.message.answer(prompt)
    finally:
        await callback.answer()


@requests.message(RequestFlow.review, F.text)
async def review_text(message: Message, state: FSMContext, parser: BookRequestParser) -> None:
    try:
        parsed = await parser.parse(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    changes = {}
    for key in (
        "format",
        "edition",
        "language",
        "maximum_budget_minor",
        "latest_delivery_date",
        "minimum_savings_minor",
        "minimum_savings_percent",
    ):
        value = getattr(parsed, key)
        if value is not None:
            changes[key] = (
                value.value
                if isinstance(value, BookFormat)
                else value.isoformat()
                if hasattr(value, "isoformat")
                else value
            )
    if parsed.title and not changes:
        changes["title"] = parsed.title
    if not changes:
        await message.answer(
            "Tell me what to change, such as 'hardcover' or 'under S$30', or use Edit Preferences.",
            reply_markup=REVIEW,
        )
        return
    data = await state.get_data()
    draft = {**data["draft"], **changes}
    if "minimum_savings_percent" in changes and "minimum_savings_minor" not in changes:
        draft["minimum_savings_minor"] = 0
    elif "minimum_savings_minor" in changes and "minimum_savings_percent" not in changes:
        draft.pop("minimum_savings_percent", None)
    await state.update_data(draft=draft)
    await show_review(message, state)


@requests.callback_query(F.data.startswith("q:"))
async def request_action(
    callback: CallbackQuery,
    state: FSMContext,
    backend: BackendClient,
    merchant_search: MerchantSearchClient | None = None,
) -> None:
    answered = False
    try:
        if not await callback_is_private(callback):
            return
        _, action, request_id = callback.data.split(":", 2)
        item = await backend.get_book_request(callback.from_user.id, request_id)
        if action == "cancel":
            await backend.cancel_book_request(callback.from_user.id, request_id)
            await callback.message.answer("Request cancelled.")
        elif action == "search":
            if not getattr(backend, "supports_live_search", False):
                await callback.message.answer("Live store search is unavailable in this mode.")
                return
            await callback.answer()
            answered = True
            await send_live_search(callback.message, item, merchant_search)
        elif action == "edit":
            if not getattr(backend, "supports_update", True):
                await callback.message.answer(
                    "The connected backend cannot edit a submitted request. Cancel it and create a new one."
                )
                return
            await clear_active_draft(state)
            await state.update_data(
                draft=item.model_dump(
                    mode="json",
                    exclude={
                        "request_id",
                        "telegram_user_id",
                        "status",
                        "created_at",
                        "updated_at",
                    },
                ),
                editing_request_id=request_id,
            )
            await show_review(callback.message, state)
    except (ValueError, BackendError) as exc:
        await callback.message.answer(
            str(exc) if isinstance(exc, Conflict) else "This request isn't available to edit."
        )
    finally:
        if not answered:
            await callback.answer()
