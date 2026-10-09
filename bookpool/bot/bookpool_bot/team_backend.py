"""Adapter for the current Person 4 backend contract in backend/docs/INTEGRATION.md."""

import json
import logging
import os
from datetime import date, datetime, time
from pathlib import Path

import httpx

from bookpool_bot.backend import BackendClient, BackendError, Conflict, NotFound
from bookpool_bot.models import (
    BookFormat,
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
from bookpool_bot.utils import SGT, now

log = logging.getLogger(__name__)


class TeamBackendClient(BackendClient):
    """Maps Telegram IDs and bot models to the backend's service API.

    The state file retains the Telegram-to-backend mapping, bot-only preferences,
    and delivered event cursor across bot restarts. The backend remains authoritative
    for requests, proposals, approvals, payments, and orders.
    """

    requires_budget = True
    supports_update = False
    decline_cancels_group = True
    supports_live_search = True
    show_catalog_on_submit = False
    supports_contributions = True

    def __init__(self, base_url: str, api_key: str, state_path: str | Path):
        if not api_key:
            raise ValueError("The team backend requires BACKEND_API_KEY")
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        self.state_path = Path(state_path)
        saved = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.user_ids: dict[int, str] = {
            int(key): value for key, value in saved.get("user_ids", {}).items()
        }
        self.preferences: dict[int, User] = {
            int(key): User.model_validate(value)
            for key, value in saved.get("preferences", {}).items()
        }
        self.cursor = int(saved.get("cursor", 0))
        self._active_id: int | None = None
        self._active_events: list[NotificationEvent] = []
        self._acked: set[str] = set()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "user_ids": self.user_ids,
            "preferences": {
                user_id: preference.model_dump(mode="json")
                for user_id, preference in self.preferences.items()
            },
            "cursor": self.cursor,
        }
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload))
        temporary.replace(self.state_path)

    async def close(self) -> None:
        await self.client.aclose()

    async def start_sandbox_enrollment(self, telegram_user_id: int) -> str:
        user_id = await self._backend_user_id(telegram_user_id)
        health = await self._request("GET", "/health")
        if health["payment_mode"] != "reap_sandbox":
            raise Conflict("The backend is currently running simulated checkout only.")
        groups = await self._request("GET", "/v1/groups", params={"limit": 500})
        if not any(
            group["purchaser_id"] == user_id and group["state"] in {"PROPOSED", "READY"}
            for group in groups
        ):
            raise Conflict(
                "Only the designated purchaser of an active group can set up the sandbox card."
            )
        result = await self._request(
            "POST",
            f"/v1/users/{user_id}/enrollments",
            headers={"Idempotency-Key": f"telegram-sandbox-{user_id}"},
            json={
                "email": os.getenv("BOOKPOOL_PURCHASER_EMAIL", "bookpool.test@example.com"),
                "return_url": health["public_base_url"].rstrip("/") + "/payment-return",
            },
        )
        return (result.get("nextAction") or {}).get("url") or ""

    async def _request(self, method: str, path: str, **kwargs):
        try:
            response = await self.client.request(method, path, **kwargs)
            if response.status_code in {403, 404}:
                raise NotFound("This item is not available to you.")
            if response.status_code in {409, 410, 412, 422}:
                detail = response.json().get("detail", {})
                if isinstance(detail, dict):
                    raise Conflict(detail.get("message", "This item changed or is unavailable."))
                raise Conflict("Some details need correction before continuing.")
            response.raise_for_status()
            return response.json() if response.content else None
        except httpx.HTTPError as exc:
            raise BackendError("The BookPool backend is temporarily unavailable.") from exc

    async def _backend_user_id(self, telegram_user_id: int) -> str:
        if telegram_user_id not in self.user_ids:
            await self.upsert_user(User(telegram_user_id=telegram_user_id))
        return self.user_ids[telegram_user_id]

    async def upsert_user(self, user: User) -> User:
        response = await self._request(
            "POST",
            "/v1/users",
            json={"telegram_id": str(user.telegram_user_id), "display_name": "BookPool reader"},
        )
        self.user_ids[user.telegram_user_id] = response["id"]
        previous = self.preferences.get(user.telegram_user_id)
        merged = User.model_validate(
            {**(previous.model_dump() if previous else {}), **user.model_dump(exclude_unset=True)}
        )
        self.preferences[user.telegram_user_id] = merged
        self._save()
        return merged

    @staticmethod
    def _deadline(value: date) -> str:
        return datetime.combine(value, time(23, 59, 59), tzinfo=SGT).isoformat()

    @staticmethod
    def _date(value: str) -> date:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(SGT).date()

    def _request_model(self, raw: dict, telegram_user_id: int) -> BookRequest:
        status = (
            RequestStatus.CANCELLED
            if raw["status"] == "CANCELLED"
            else RequestStatus.ORDER_PLACED
            if raw["status"] == "ORDERED"
            else RequestStatus.GROUP_AVAILABLE
            if raw.get("group_id")
            else RequestStatus.SEARCHING
        )
        edition = raw.get("edition")
        format_value = edition if edition in {"paperback", "hardcover"} else "any"
        created_at = datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00"))
        return BookRequest(
            request_id=raw["id"],
            telegram_user_id=telegram_user_id,
            title=raw["title"],
            isbn=raw.get("isbn"),
            edition=edition,
            format=BookFormat(format_value),
            latest_delivery_date=self._date(raw["latest_delivery_at"]),
            maximum_budget_minor=raw["budget_minor"],
            minimum_savings_minor=raw["min_savings_minor"],
            currency=raw["currency"],
            pickup_location_id="ntu",
            status=status,
            created_at=created_at,
            updated_at=created_at,
        )

    async def create_book_request(self, user_id: int, data: BookRequestInput) -> BookRequest:
        if not data.title:
            raise Conflict(
                "This backend needs a book title as well as any ISBN. Edit the book field."
            )
        if data.maximum_budget_minor is None:
            raise Conflict("This backend requires a maximum total budget. Edit your budget.")
        if data.minimum_savings_percent is not None:
            raise Conflict(
                "This backend currently accepts minimum savings in S$, not percent. Edit savings."
            )
        backend_user_id = await self._backend_user_id(user_id)
        raw = await self._request(
            "POST",
            "/v1/requests",
            json={
                "user_id": backend_user_id,
                "title": data.title,
                "isbn": data.isbn,
                "edition": data.edition
                or (data.format.value if data.format != BookFormat.ANY else None),
                "quantity": 1,
                "budget_minor": data.maximum_budget_minor,
                "min_savings_minor": data.minimum_savings_minor,
                "currency": "SGD",
                "latest_delivery_at": self._deadline(data.latest_delivery_date),
                "pickup_location": "NTU shared collection point",
            },
        )
        return self._request_model(raw, user_id)

    async def update_book_request(
        self, user_id: int, request_id: str, data: BookRequestInput
    ) -> BookRequest:
        raise Conflict(
            "The connected backend cannot edit submitted requests yet. Cancel and create a new request."
        )

    async def cancel_book_request(self, user_id: int, request_id: str) -> BookRequest:
        await self.get_book_request(user_id, request_id)
        raw = await self._request("POST", f"/v1/requests/{request_id}/cancel")
        return self._request_model(raw, user_id)

    async def list_book_requests(self, user_id: int) -> list[BookRequest]:
        backend_user_id = await self._backend_user_id(user_id)
        raw = await self._request("GET", "/v1/requests", params={"limit": 500})
        return [
            self._request_model(item, user_id) for item in raw if item["user_id"] == backend_user_id
        ]

    async def get_book_request(self, user_id: int, request_id: str) -> BookRequest:
        item = next(
            (r for r in await self.list_book_requests(user_id) if r.request_id == request_id), None
        )
        if item is None:
            raise NotFound("Request not found.")
        return item

    def _proposal_model(self, raw: dict, telegram_user_id: int) -> GroupProposal:
        backend_user_id = self.user_ids.get(telegram_user_id)
        mine = [p for p in raw["participants"] if p["user_id"] == backend_user_id]
        if not mine:
            raise NotFound("This group is not available to you.")
        status = (
            ProposalStatus.AWAITING_APPROVAL
            if raw["state"] in {"PROPOSED", "READY"}
            else ProposalStatus.EXPIRED
        )
        individual = sum(p["individual_total_minor"] for p in mine)
        group_total = sum(p["amount_minor"] for p in mine)
        return GroupProposal(
            proposal_id=raw["id"],
            request_id=mine[0]["request_id"],
            telegram_user_id=telegram_user_id,
            merchant_name=raw["merchant"],
            participant_count=len({p["user_id"] for p in raw["participants"]}),
            individual_total_minor=individual,
            group_total_minor=group_total,
            savings_minor=individual - group_total,
            currency=raw["currency"],
            free_shipping_unlocked=None,
            estimated_delivery_date=self._date(raw["delivery_at"]),
            quote_version=raw["version"],
            expires_at=datetime.fromisoformat(raw["expires_at"].replace("Z", "+00:00")),
            status=status,
            recommendation_reason=raw.get("reason"),
            payment_status=", ".join(sorted({p["payment_state"] for p in mine})),
            is_estimate=not bool(raw.get("quote_id")),
            book_titles=[p["title"] for p in mine],
            user_approved=all(p["approved_version"] == raw["version"] for p in mine),
        )

    async def list_user_group_proposals(self, user_id: int) -> list[GroupProposal]:
        await self._backend_user_id(user_id)
        raw = await self._request("GET", "/v1/groups", params={"limit": 500})
        result = []
        for group in raw:
            try:
                proposal = self._proposal_model(group, user_id)
                if proposal.status != ProposalStatus.EXPIRED:
                    result.append(proposal)
            except NotFound:
                continue
        return result

    async def get_group_proposal(self, user_id: int, proposal_id: str) -> GroupProposal:
        await self._backend_user_id(user_id)
        raw = await self._request("GET", f"/v1/groups/{proposal_id}")
        return self._proposal_model(raw, user_id)

    async def submit_group_decision(
        self,
        user_id: int,
        proposal_id: str,
        quote_version: int,
        decision: Decision,
        idempotency_key: str,
    ) -> DecisionResult:
        current = await self.get_group_proposal(user_id, proposal_id)
        if (
            current.quote_version != quote_version
            or current.status == ProposalStatus.EXPIRED
            or current.expires_at <= now()
        ):
            raise Conflict("This proposal changed or expired. Review its current version.")
        backend_user_id = await self._backend_user_id(user_id)
        if decision == Decision.APPROVE:
            raw = await self._request(
                "POST",
                f"/v1/groups/{proposal_id}/participants/{backend_user_id}/approve",
                headers={"Idempotency-Key": idempotency_key},
                json={"version": quote_version},
            )
            message = "Your approval was recorded for this group version. No payment or order has been made."
        elif decision == Decision.DECLINE:
            raw = await self._request(
                "POST",
                f"/v1/groups/{proposal_id}/participants/{backend_user_id}/withdraw",
                headers={"Idempotency-Key": idempotency_key},
                json={"version": quote_version},
            )
            message = "You left this group. The backend cancelled the proposal for all participants so it can be rebuilt."
        elif decision == Decision.CONTRIBUTE:
            if not current.user_approved:
                raise Conflict("Approve this group quote before confirming your contribution.")
            raw = await self._request(
                "POST",
                f"/v1/groups/{proposal_id}/participants/{backend_user_id}/authorize",
                headers={"Idempotency-Key": idempotency_key},
                json={"version": quote_version, "outcome": "success"},
            )
            message = (
                "Your simulated contribution is authorized. No money was charged. "
                "BookPool will run a simulated group checkout when everyone is ready."
            )
        else:
            raise Conflict(
                "The connected backend accepts final approval or withdrawal only for this group."
            )
        return DecisionResult(
            recorded=True, message=message, proposal=self._proposal_model(raw, user_id)
        )

    async def fetch_pending_events(self, limit: int = 20) -> list[NotificationEvent]:
        if self._active_id is not None:
            return [event for event in self._active_events if event.event_id not in self._acked]
        kinds = {
            "PROPOSAL_CREATED": "final_approval_requested",
            "REAP_QUOTE_READY": "final_approval_requested",
            "GROUP_EXPIRED": "proposal_expired",
            "GROUP_CANCELLED": "proposal_expired",
            "ORDER_PLACED": "order_placed",
            "ORDER_FAILED": "payment_failed",
            "PAYMENT_APPROVAL_REQUIRED": "payment_approval_required",
        }
        for _ in range(limit):
            page = await self._request(
                "GET", "/v1/events", params={"after": self.cursor, "limit": 1}
            )
            if not page["events"]:
                return []
            raw = page["events"][0]
            if raw["kind"] not in kinds or not raw.get("group_id"):
                self.cursor = raw["id"]
                self._save()
                continue
            group = await self._request("GET", f"/v1/groups/{raw['group_id']}")
            event_type = kinds[raw["kind"]]
            if (
                raw["kind"] == "REAP_QUOTE_READY"
                and (raw.get("payload") or {}).get("version", 0) > 2
            ):
                event_type = "quote_changed"
            events = []
            for telegram_user_id, backend_user_id in self.user_ids.items():
                mine = [p for p in group["participants"] if p["user_id"] == backend_user_id]
                if mine:
                    payload = dict(raw.get("payload") or {})
                    if raw["kind"] == "PAYMENT_APPROVAL_REQUIRED":
                        payload["is_purchaser"] = backend_user_id == group["purchaser_id"]
                        if payload["is_purchaser"]:
                            payload["approval_url"] = (group.get("order") or {}).get("approval_url")
                    events.append(
                        NotificationEvent(
                            event_id=f"{raw['id']}:{telegram_user_id}",
                            event_type=event_type,
                            telegram_user_id=telegram_user_id,
                            request_id=mine[0]["request_id"],
                            proposal_id=raw["group_id"],
                            occurred_at=datetime.fromisoformat(
                                raw["created_at"].replace("Z", "+00:00")
                            ),
                            payload=payload,
                        )
                    )
            if not events:
                log.warning("No known Telegram recipient for backend event %s", raw["id"])
                return []
            self._active_id = raw["id"]
            self._active_events = events
            self._acked = set()
            return events
        return []

    async def acknowledge_event(self, event_id: str) -> None:
        if self._active_id is None:
            return
        self._acked.add(event_id)
        if all(event.event_id in self._acked for event in self._active_events):
            self.cursor = self._active_id
            self._active_id = None
            self._active_events = []
            self._acked = set()
            self._save()
