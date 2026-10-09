from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bookpool_bot.backend import BackendClient, BackendError, Conflict, MockBackendClient, NotFound
from bookpool_bot.keyboards import MAIN_MENU, proposal_actions
from bookpool_bot.models import Decision, ProposalStatus, RequestStatus
from bookpool_bot.presentation import proposal_details, proposal_text
from bookpool_bot.team_backend import TeamBackendClient
from bookpool_bot.utils import now

from .common import callback_is_private, is_private

groups = Router(name="groups")


@groups.message(Command("sandboxcard"))
async def sandbox_card(message: Message, backend: BackendClient) -> None:
    if not await is_private(message):
        return
    if not isinstance(backend, TeamBackendClient):
        await message.answer("Reap sandbox card setup is available in team mode.")
        return
    try:
        url = await backend.start_sandbox_enrollment(message.from_user.id)
        await message.answer(
            "Open this Reap sandbox page and enter the test card there:\n" + url
            if url
            else "Your Reap sandbox card enrollment is already active."
        )
    except (Conflict, BackendError) as exc:
        await message.answer(str(exc))


@groups.message(Command("groups"))
async def show_groups(message: Message, backend: BackendClient, user_id: int | None = None) -> None:
    if not await is_private(message):
        return
    actor = user_id or message.from_user.id
    proposals = await backend.list_user_group_proposals(actor)
    if not proposals:
        await message.answer(
            "No group offers yet. We're still looking for compatible buyers.",
            reply_markup=MAIN_MENU,
        )
        return
    for proposal in proposals:
        request = await backend.get_book_request(actor, proposal.request_id)
        await message.answer(
            proposal_text(proposal, request.title or request.isbn),
            reply_markup=proposal_actions(
                proposal.proposal_id,
                proposal.quote_version,
                proposal.status.value == "awaiting_approval",
                "Leave group (cancels proposal)"
                if getattr(backend, "decline_cancels_group", False)
                else "Decline",
                proposal.user_approved,
                getattr(backend, "supports_contributions", False)
                and proposal.user_approved
                and proposal.payment_status != "AUTHORIZED",
            ),
        )


@groups.callback_query(F.data == "m:groups")
async def menu_groups(callback: CallbackQuery, backend: BackendClient) -> None:
    try:
        if not await callback_is_private(callback):
            return
        await show_groups(callback.message, backend, callback.from_user.id)
    finally:
        await callback.answer()


@groups.callback_query(F.data.startswith("p:"))
async def proposal_action(callback: CallbackQuery, backend: BackendClient) -> None:
    try:
        if not await callback_is_private(callback):
            return
        _, action, proposal_id, raw_version = callback.data.split(":", 3)
        version = int(raw_version)
        proposal = await backend.get_group_proposal(callback.from_user.id, proposal_id)
        if (
            proposal.quote_version != version
            or proposal.expires_at <= now()
            or proposal.status.value in {"expired", "superseded"}
        ):
            await callback.message.answer(
                "This offer has expired or changed. Use /groups to review the latest quote."
            )
            return
        request = await backend.get_book_request(callback.from_user.id, proposal.request_id)
        if action == "details":
            await callback.message.answer(
                proposal_details(proposal, request.title or request.isbn),
                reply_markup=proposal_actions(
                    proposal_id,
                    version,
                    proposal.status.value == "awaiting_approval",
                    "Leave group (cancels proposal)"
                    if getattr(backend, "decline_cancels_group", False)
                    else "Decline",
                    proposal.user_approved,
                    getattr(backend, "supports_contributions", False)
                    and proposal.user_approved
                    and proposal.payment_status != "AUTHORIZED",
                ),
            )
            return
        if action not in {"ready", "wait", "approve", "contribute", "decline"}:
            await callback.message.answer("That action isn't available.")
            return
        if action == "approve" and proposal.status.value != "awaiting_approval":
            await callback.message.answer(
                "Final approval isn't open yet. You can express readiness for this offer."
            )
            return
        if action == "approve" and proposal.user_approved:
            await callback.message.answer("You already approved this quote version.")
            return
        if action == "contribute" and (
            not getattr(backend, "supports_contributions", False) or not proposal.user_approved
        ):
            await callback.message.answer("Approve this quote before confirming a contribution.")
            return
        # Stable per user/quote/action so repeated Telegram deliveries cannot double-submit.
        key = f"tg:{callback.from_user.id}:{proposal_id}:{version}:{action}"
        result = await backend.submit_group_decision(
            callback.from_user.id, proposal_id, version, Decision(action), key
        )
        updated = result.proposal
        markup = (
            proposal_actions(
                updated.proposal_id,
                updated.quote_version,
                updated.status.value == "awaiting_approval",
                "Leave group (cancels proposal)"
                if getattr(backend, "decline_cancels_group", False)
                else "Decline",
                updated.user_approved,
                getattr(backend, "supports_contributions", False)
                and updated.user_approved
                and updated.payment_status != "AUTHORIZED",
            )
            if updated.status.value not in {"expired", "superseded"}
            else None
        )
        await callback.message.answer(result.message, reply_markup=markup)
    except (ValueError, NotFound):
        await callback.message.answer("This offer isn't available to you.")
    except Conflict as exc:
        await callback.message.answer(str(exc))
    except BackendError:
        await callback.message.answer("I couldn't record your decision. Please try again shortly.")
    finally:
        await callback.answer()


@groups.message(Command("demo"))
async def demo(message: Message, backend: BackendClient) -> None:
    if not isinstance(backend, MockBackendClient) or not await is_private(message):
        await message.answer("Demo events are only available in mock mode.")
        return
    parts = (message.text or "").split()
    action = parts[1] if len(parts) > 1 else "help"
    items = await backend.list_book_requests(message.from_user.id)
    if not items:
        await message.answer("Create a request first with /new.")
        return
    request_id = items[0].request_id
    if action in {"group", "recommend", "reprice", "approval"}:
        proposal = await backend.demo_group(
            message.from_user.id,
            request_id,
            recommended=action in {"recommend", "approval"},
            revised=action == "reprice",
        )
        if action == "approval":
            backend.proposals[proposal.proposal_id] = proposal.model_copy(
                update={"status": ProposalStatus.AWAITING_APPROVAL}
            )
            proposal = backend.proposals[proposal.proposal_id]
            saved_request = await backend.get_book_request(message.from_user.id, request_id)
            backend.requests[request_id] = saved_request.model_copy(
                update={"status": RequestStatus.AWAITING_APPROVAL}
            )
        await backend.demo_event(
            {
                "group": "group_found",
                "recommend": "buy_now_recommended",
                "reprice": "quote_changed",
                "approval": "final_approval_requested",
            }[action],
            message.from_user.id,
            request_id,
            proposal.proposal_id,
        )
        await message.answer(
            "Fictional demo event queued. The notification dispatcher will deliver it shortly."
        )
    elif action == "complete":
        await backend.demo_complete(message.from_user.id, request_id)
        await message.answer("Fictional completion event queued.")
    else:
        await message.answer(
            "Demo commands: /demo group, /demo recommend, /demo approval, /demo reprice, /demo complete. All prices and statuses are fictional."
        )
