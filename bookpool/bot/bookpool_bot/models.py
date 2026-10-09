from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class BookFormat(StrEnum):
    ANY = "any"
    PAPERBACK = "paperback"
    HARDCOVER = "hardcover"


class RequestStatus(StrEnum):
    SEARCHING = "searching"
    WAITING = "waiting_for_group"
    GROUP_AVAILABLE = "group_available"
    AWAITING_APPROVAL = "awaiting_approval"
    PAYMENT_PENDING = "payment_pending"
    ORDER_PLACED = "order_placed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ProposalStatus(StrEnum):
    AVAILABLE = "available"
    RECOMMENDED = "recommended"
    AWAITING_APPROVAL = "awaiting_approval"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class Decision(StrEnum):
    READY = "ready"
    WAIT = "wait"
    APPROVE = "approve"
    CONTRIBUTE = "contribute"
    DECLINE = "decline"


class User(BaseModel):
    telegram_user_id: int
    pickup_location_id: str = "ntu"
    pickup_confirmed: bool = False
    preference: str = "lower_cost"


class BookRequestInput(BaseModel):
    title: str | None = None
    isbn: str | None = None
    author: str | None = None
    edition: str | None = None
    language: str = "English"
    format: BookFormat = BookFormat.ANY
    latest_delivery_date: date
    maximum_budget_minor: int | None = Field(default=None, gt=0)
    minimum_savings_minor: int = Field(default=0, ge=0)
    minimum_savings_percent: Decimal | None = Field(default=None, ge=0, le=100)
    preference: str = "lower_cost"
    currency: str = "SGD"
    pickup_location_id: str = "ntu"

    @model_validator(mode="after")
    def require_book(self) -> "BookRequestInput":
        if not self.title and not self.isbn:
            raise ValueError("A title or ISBN is required")
        return self


class BookRequest(BookRequestInput):
    request_id: str
    telegram_user_id: int
    status: RequestStatus = RequestStatus.SEARCHING
    created_at: datetime
    updated_at: datetime


class GroupProposal(BaseModel):
    proposal_id: str
    request_id: str
    telegram_user_id: int
    merchant_name: str
    participant_count: int = Field(ge=1)
    individual_total_minor: int = Field(ge=0)
    item_price_minor: int | None = Field(default=None, ge=0)
    shipping_minor: int | None = Field(default=None, ge=0)
    other_fees_minor: int | None = Field(default=None, ge=0)
    group_total_minor: int = Field(ge=0)
    savings_minor: int
    currency: str = "SGD"
    free_shipping_unlocked: bool | None = None
    estimated_delivery_date: date
    quote_version: int = Field(ge=1)
    expires_at: datetime
    status: ProposalStatus
    recommendation_reason: str | None = None
    payment_status: str = "not_started"
    is_estimate: bool = True
    book_titles: list[str] = Field(default_factory=list)
    user_approved: bool = False

    @model_validator(mode="after")
    def validate_quote(self) -> "GroupProposal":
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("Proposal expiry must include a timezone")
        components = (self.item_price_minor, self.shipping_minor, self.other_fees_minor)
        if (
            all(value is not None for value in components)
            and sum(components) != self.group_total_minor
        ):
            raise ValueError("Proposal price breakdown does not match total")
        if self.individual_total_minor - self.group_total_minor != self.savings_minor:
            raise ValueError("Proposal savings do not match quoted totals")
        return self


class NotificationEvent(BaseModel):
    event_id: str
    event_type: str
    telegram_user_id: int
    request_id: str | None = None
    proposal_id: str | None = None
    occurred_at: datetime
    payload: dict = Field(default_factory=dict)


class DecisionResult(BaseModel):
    recorded: bool
    message: str
    proposal: GroupProposal | None = None
