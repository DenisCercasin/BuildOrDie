import asyncio
from abc import ABC, abstractmethod
from datetime import timedelta
from uuid import uuid4

import httpx
from pydantic import ValidationError

from bookpool_bot.models import (
    BookRequest,
    BookRequestInput,
    Decision,
    DecisionResult,
    GroupProposal,
    NotificationEvent,
    ProposalStatus,
    RequestStatus,
    User,
)
from bookpool_bot.utils import now, today


class BackendError(Exception):
    pass


class NotFound(BackendError):
    pass


class Conflict(BackendError):
    pass


class BackendClient(ABC):
    @abstractmethod
    async def upsert_user(self, user: User) -> User: ...

    @abstractmethod
    async def create_book_request(self, user_id: int, data: BookRequestInput) -> BookRequest: ...

    @abstractmethod
    async def update_book_request(
        self, user_id: int, request_id: str, data: BookRequestInput
    ) -> BookRequest: ...

    @abstractmethod
    async def cancel_book_request(self, user_id: int, request_id: str) -> BookRequest: ...

    @abstractmethod
    async def list_book_requests(self, user_id: int) -> list[BookRequest]: ...

    @abstractmethod
    async def get_book_request(self, user_id: int, request_id: str) -> BookRequest: ...

    @abstractmethod
    async def list_user_group_proposals(self, user_id: int) -> list[GroupProposal]: ...

    @abstractmethod
    async def get_group_proposal(self, user_id: int, proposal_id: str) -> GroupProposal: ...

    @abstractmethod
    async def submit_group_decision(
        self,
        user_id: int,
        proposal_id: str,
        quote_version: int,
        decision: Decision,
        idempotency_key: str,
    ) -> DecisionResult: ...

    @abstractmethod
    async def fetch_pending_events(self, limit: int = 20) -> list[NotificationEvent]: ...

    @abstractmethod
    async def acknowledge_event(self, event_id: str) -> None: ...


class MockBackendClient(BackendClient):
    """Process-local fictional backend. State resets when the bot restarts."""

    def __init__(self) -> None:
        self.users: dict[int, User] = {}
        self.requests: dict[str, BookRequest] = {}
        self.proposals: dict[str, GroupProposal] = {}
        self.events: dict[str, NotificationEvent] = {}
        self.decisions: dict[str, DecisionResult] = {}
        self.lock = asyncio.Lock()

    async def upsert_user(self, user: User) -> User:
        previous = self.users.get(user.telegram_user_id)
        if previous:
            user = User.model_validate(
                {**previous.model_dump(), **user.model_dump(exclude_unset=True)}
            )
        self.users[user.telegram_user_id] = user
        return user

    async def create_book_request(self, user_id: int, data: BookRequestInput) -> BookRequest:
        stamp = now()
        request = BookRequest(
            **data.model_dump(),
            request_id=uuid4().hex[:12],
            telegram_user_id=user_id,
            created_at=stamp,
            updated_at=stamp,
        )
        self.requests[request.request_id] = request
        return request

    async def update_book_request(
        self, user_id: int, request_id: str, data: BookRequestInput
    ) -> BookRequest:
        request = await self.get_book_request(user_id, request_id)
        if request.status in {
            RequestStatus.CANCELLED,
            RequestStatus.ORDER_PLACED,
            RequestStatus.EXPIRED,
        }:
            raise Conflict("This request can no longer be edited.")
        updated = request.model_copy(update={**data.model_dump(), "updated_at": now()})
        self.requests[request_id] = updated
        for proposal in self.proposals.values():
            if proposal.request_id == request_id and proposal.status not in {
                ProposalStatus.EXPIRED,
                ProposalStatus.SUPERSEDED,
            }:
                self.proposals[proposal.proposal_id] = proposal.model_copy(
                    update={"status": ProposalStatus.SUPERSEDED}
                )
        return updated

    async def cancel_book_request(self, user_id: int, request_id: str) -> BookRequest:
        request = await self.get_book_request(user_id, request_id)
        if request.status == RequestStatus.ORDER_PLACED:
            raise Conflict("An order has already been placed; contact support for help.")
        updated = request.model_copy(
            update={"status": RequestStatus.CANCELLED, "updated_at": now()}
        )
        self.requests[request_id] = updated
        return updated

    async def list_book_requests(self, user_id: int) -> list[BookRequest]:
        return sorted(
            (r for r in self.requests.values() if r.telegram_user_id == user_id),
            key=lambda r: r.created_at,
            reverse=True,
        )

    async def get_book_request(self, user_id: int, request_id: str) -> BookRequest:
        request = self.requests.get(request_id)
        if request is None or request.telegram_user_id != user_id:
            raise NotFound("Request not found.")
        return request

    async def list_user_group_proposals(self, user_id: int) -> list[GroupProposal]:
        return [
            p
            for p in self.proposals.values()
            if p.telegram_user_id == user_id and p.status != ProposalStatus.SUPERSEDED
        ]

    async def get_group_proposal(self, user_id: int, proposal_id: str) -> GroupProposal:
        proposal = self.proposals.get(proposal_id)
        if proposal is None or proposal.telegram_user_id != user_id:
            raise NotFound("Offer not found.")
        return proposal

    async def submit_group_decision(
        self,
        user_id: int,
        proposal_id: str,
        quote_version: int,
        decision: Decision,
        idempotency_key: str,
    ) -> DecisionResult:
        async with self.lock:
            if idempotency_key in self.decisions:
                return self.decisions[idempotency_key]
            proposal = await self.get_group_proposal(user_id, proposal_id)
            if (
                proposal.quote_version != quote_version
                or proposal.status in {ProposalStatus.EXPIRED, ProposalStatus.SUPERSEDED}
                or proposal.expires_at <= now()
            ):
                raise Conflict("This offer has expired or changed. Please review the latest quote.")
            if decision == Decision.APPROVE and proposal.status != ProposalStatus.AWAITING_APPROVAL:
                raise Conflict("Final approval is not open for this quote.")
            request = await self.get_book_request(user_id, proposal.request_id)
            if (
                request.maximum_budget_minor is not None
                and proposal.group_total_minor > request.maximum_budget_minor
                and decision in {Decision.READY, Decision.APPROVE}
            ):
                raise Conflict(
                    "This quote exceeds your maximum budget. Change your budget to proceed."
                )
            result = DecisionResult(
                recorded=True,
                message={
                    Decision.READY: "Your willingness to buy was recorded. No payment or order has been made.",
                    Decision.WAIT: "We'll keep looking for a better opportunity.",
                    Decision.APPROVE: "Your approval was recorded for this quote. Payment has not been taken.",
                    Decision.DECLINE: "You declined this offer. Your request remains open.",
                }[decision],
                proposal=proposal,
            )
            self.decisions[idempotency_key] = result
            return result

    async def fetch_pending_events(self, limit: int = 20) -> list[NotificationEvent]:
        return list(self.events.values())[:limit]

    async def acknowledge_event(self, event_id: str) -> None:
        self.events.pop(event_id, None)

    async def demo_group(
        self, user_id: int, request_id: str, *, recommended: bool = False, revised: bool = False
    ) -> GroupProposal:
        request = await self.get_book_request(user_id, request_id)
        if request.status == RequestStatus.CANCELLED:
            raise Conflict("The request was cancelled.")
        previous = next(
            (
                p
                for p in self.proposals.values()
                if p.request_id == request_id and p.status != ProposalStatus.SUPERSEDED
            ),
            None,
        )
        if previous:
            self.proposals[previous.proposal_id] = previous.model_copy(
                update={"status": ProposalStatus.SUPERSEDED}
            )
        version = previous.quote_version + 1 if previous else 1
        group = 2820 if revised else 2500
        proposal = GroupProposal(
            proposal_id=uuid4().hex[:12],
            request_id=request_id,
            telegram_user_id=user_id,
            merchant_name="Kinokuniya Singapore (fictional demo quote)",
            participant_count=3 if revised else 4,
            individual_total_minor=3190,
            item_price_minor=group,
            shipping_minor=0,
            group_total_minor=group,
            savings_minor=3190 - group,
            free_shipping_unlocked=True,
            estimated_delivery_date=min(request.latest_delivery_date, today() + timedelta(days=5)),
            quote_version=version,
            expires_at=now() + timedelta(hours=2),
            status=ProposalStatus.RECOMMENDED if recommended else ProposalStatus.AVAILABLE,
            recommendation_reason="Demo decision engine: additional waiting is unlikely to improve savings materially."
            if recommended
            else None,
        )
        self.proposals[proposal.proposal_id] = proposal
        self.requests[request_id] = request.model_copy(
            update={"status": RequestStatus.GROUP_AVAILABLE, "updated_at": now()}
        )
        return proposal

    async def demo_event(
        self, event_type: str, user_id: int, request_id: str, proposal_id: str | None = None
    ) -> NotificationEvent:
        event = NotificationEvent(
            event_id=uuid4().hex,
            event_type=event_type,
            telegram_user_id=user_id,
            request_id=request_id,
            proposal_id=proposal_id,
            occurred_at=now(),
        )
        self.events[event.event_id] = event
        return event

    async def demo_complete(self, user_id: int, request_id: str) -> None:
        request = await self.get_book_request(user_id, request_id)
        self.requests[request_id] = request.model_copy(
            update={"status": RequestStatus.ORDER_PLACED, "updated_at": now()}
        )
        await self.demo_event("order_placed", user_id, request_id)


class HttpBackendClient(BackendClient):
    """Proposed REST contract. See docs/bot_api_contract.md."""

    def __init__(self, base_url: str, api_key: str):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=10,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs):
        try:
            response = await self.client.request(method, path, **kwargs)
            if response.status_code == 404:
                raise NotFound("Resource not found.")
            if response.status_code in {409, 410, 412}:
                raise Conflict("This item changed or expired. Please refresh it.")
            response.raise_for_status()
            return response.json() if response.content else None
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            raise BackendError("The BookPool service is temporarily unavailable.") from exc

    async def upsert_user(self, user: User) -> User:
        return User.model_validate(
            await self._request(
                "PUT",
                f"/v1/users/{user.telegram_user_id}",
                json=user.model_dump(mode="json", exclude_unset=True),
            )
        )

    async def create_book_request(self, user_id: int, data: BookRequestInput) -> BookRequest:
        return BookRequest.model_validate(
            await self._request(
                "POST",
                "/v1/book-requests",
                json={**data.model_dump(mode="json"), "telegram_user_id": user_id},
            )
        )

    async def update_book_request(
        self, user_id: int, request_id: str, data: BookRequestInput
    ) -> BookRequest:
        return BookRequest.model_validate(
            await self._request(
                "PUT",
                f"/v1/users/{user_id}/book-requests/{request_id}",
                json=data.model_dump(mode="json"),
            )
        )

    async def cancel_book_request(self, user_id: int, request_id: str) -> BookRequest:
        return BookRequest.model_validate(
            await self._request("POST", f"/v1/users/{user_id}/book-requests/{request_id}/cancel")
        )

    async def list_book_requests(self, user_id: int) -> list[BookRequest]:
        return [
            BookRequest.model_validate(x)
            for x in await self._request("GET", f"/v1/users/{user_id}/book-requests")
        ]

    async def get_book_request(self, user_id: int, request_id: str) -> BookRequest:
        return BookRequest.model_validate(
            await self._request("GET", f"/v1/users/{user_id}/book-requests/{request_id}")
        )

    async def list_user_group_proposals(self, user_id: int) -> list[GroupProposal]:
        return [
            GroupProposal.model_validate(x)
            for x in await self._request("GET", f"/v1/users/{user_id}/group-proposals")
        ]

    async def get_group_proposal(self, user_id: int, proposal_id: str) -> GroupProposal:
        return GroupProposal.model_validate(
            await self._request("GET", f"/v1/users/{user_id}/group-proposals/{proposal_id}")
        )

    async def submit_group_decision(
        self,
        user_id: int,
        proposal_id: str,
        quote_version: int,
        decision: Decision,
        idempotency_key: str,
    ) -> DecisionResult:
        return DecisionResult.model_validate(
            await self._request(
                "POST",
                f"/v1/users/{user_id}/group-proposals/{proposal_id}/decisions",
                headers={"Idempotency-Key": idempotency_key},
                json={"quote_version": quote_version, "decision": decision.value},
            )
        )

    async def fetch_pending_events(self, limit: int = 20) -> list[NotificationEvent]:
        return [
            NotificationEvent.model_validate(x)
            for x in await self._request("GET", "/v1/bot/events", params={"limit": limit})
        ]

    async def acknowledge_event(self, event_id: str) -> None:
        await self._request("POST", f"/v1/bot/events/{event_id}/ack")
