import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from pydantic import ValidationError

from bookpool_bot.backend import BackendClient, BackendError, MockBackendClient, NotFound
from bookpool_bot.keyboards import proposal_actions
from bookpool_bot.models import NotificationEvent
from bookpool_bot.presentation import proposal_text, request_line

log = logging.getLogger(__name__)


class NotificationDispatcher:
    def __init__(self, bot: Bot, backend: BackendClient, interval: float = 5):
        self.bot = bot
        self.backend = backend
        self.interval = interval
        self.delivered: set[str] = set()
        self._last_sent: dict[int, float] = {}

    async def deliver(self, event: NotificationEvent) -> bool:
        if event.event_id in self.delivered:
            await self.backend.acknowledge_event(event.event_id)
            return True
        try:
            if event.event_type == "order_placed":
                request = await self.backend.get_book_request(
                    event.telegram_user_id, event.request_id
                )
                simulated = isinstance(self.backend, MockBackendClient) or event.payload.get(
                    "simulated", False
                )
                heading = (
                    "Simulated group checkout completed. No real funds were charged and no merchant order was placed."
                    if simulated
                    else "Your BookPool order was confirmed by the payment provider."
                )
                text = f"{heading}\n\n{request_line(request)}"
                markup = None
            elif event.event_type == "payment_approval_required":
                if event.payload.get("is_purchaser") and event.payload.get("approval_url"):
                    text = (
                        "All participants confirmed their simulated contributions. "
                        "Open this Reap sandbox page to approve the single group checkout:\n"
                        f"{event.payload['approval_url']}"
                    )
                else:
                    text = (
                        "All participants confirmed their simulated contributions. "
                        "The designated purchaser must approve the Reap sandbox checkout."
                    )
                markup = None
            elif event.proposal_id:
                proposal = await self.backend.get_group_proposal(
                    event.telegram_user_id, event.proposal_id
                )
                request = await self.backend.get_book_request(
                    event.telegram_user_id, proposal.request_id
                )
                heading = {
                    "group_found": "A group opportunity is available.",
                    "quote_changed": "Your group quote changed. Please review the new price before deciding.",
                    "price_improved": "Your group price improved. Review the updated quote.",
                    "free_shipping_unlocked": "Your group unlocked free shipping.",
                    "buy_now_recommended": "The decision engine recommends buying now.",
                    "final_approval_requested": "Final approval is requested for this quote.",
                    "proposal_expired": "This group offer has expired. Check for a new quote.",
                    "deadline_approaching": "Your latest pickup date is approaching. Review your request.",
                    "payment_failed": "The payment step failed. Check your request status for next steps.",
                }.get(event.event_type, "BookPool has an update about your group.")
                text = f"{heading}\n\n{proposal_text(proposal, request.title or request.isbn)}"
                if event.payload.get("reap_quote") and event.payload.get("is_purchaser"):
                    text += (
                        "\n\nYou are the designated purchaser. Send /sandboxcard "
                        "to set up the test card on Reap's hosted page."
                    )
                markup = (
                    None
                    if proposal.status.value in {"expired", "superseded"}
                    else proposal_actions(
                        proposal.proposal_id,
                        proposal.quote_version,
                        proposal.status.value == "awaiting_approval",
                        "Leave group (cancels proposal)"
                        if getattr(self.backend, "decline_cancels_group", False)
                        else "Decline",
                        proposal.user_approved,
                        getattr(self.backend, "supports_contributions", False)
                        and proposal.user_approved
                        and proposal.payment_status != "AUTHORIZED",
                    )
                )
            else:
                text = "BookPool has an update about your request. Use /myrequests to see its current status."
                markup = None
            elapsed = time.monotonic() - self._last_sent.get(event.telegram_user_id, 0)
            if elapsed < 1:
                await asyncio.sleep(1 - elapsed)
            await self.bot.send_message(event.telegram_user_id, text, reply_markup=markup)
            self._last_sent[event.telegram_user_id] = time.monotonic()
            self.delivered.add(event.event_id)
            await self.backend.acknowledge_event(event.event_id)
            log.info("notification_delivered event_id=%s type=%s", event.event_id, event.event_type)
            return True
        except TelegramRetryAfter as exc:
            log.warning(
                "notification_rate_limited event_id=%s retry_after=%s",
                event.event_id,
                exc.retry_after,
            )
        except (TelegramAPIError, BackendError, NotFound):
            log.exception("notification_failed event_id=%s", event.event_id)
        return False

    async def run_once(self) -> None:
        events = await self.backend.fetch_pending_events()
        for event in events:
            await self.deliver(event)

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except (BackendError, ValidationError):
                log.exception("event_poll_failed")
            await asyncio.sleep(self.interval)
