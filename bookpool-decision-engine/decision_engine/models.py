"""Shared data contracts for the BookPool decision engine.

These are the objects the other three components exchange with Person 3's engine:

* Person 1 (Telegram bot) produces ``BookRequest`` data (via the backend) and
  displays ``GroupProposal`` / ``SoloPlan`` / ``EngineEvent`` content.
* Person 2 (merchant intelligence) produces ``MerchantPolicy`` and ``MerchantOffer``.
* Person 4 (backend) stores everything, calls ``DecisionEngine.evaluate`` and turns
  approved ``GroupProposal`` objects into orders and payments.

All money is ``Decimal`` in SGD. In JSON, pydantic serialises Decimals as strings
(e.g. ``"27.90"``) so no precision is lost; send them back the same way.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _In(BaseModel):
    """Input models: tolerant of extra fields so teammates can add their own."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- inputs


class BookRequest(_In):
    """One user's open request for one book (an *open* request, not yet committed)."""

    request_id: str
    user_id: str
    book_id: str = Field(description="ISBN-13 or Person 2's canonical book id")
    title: str | None = None
    quantity: int = Field(default=1, ge=1)
    deadline: date = Field(description="Latest acceptable delivery date")
    max_budget: Decimal | None = Field(
        default=None, description="Max total the user will pay incl. their shipping share"
    )
    min_savings_to_wait: Decimal = Field(
        default=Decimal("0"),
        description="Smallest saving that makes waiting for a group worthwhile to this user",
    )
    pickup_point: str = Field(description="e.g. 'NTU-NorthSpine'. Groups share one pickup point")
    preferred_merchants: list[str] | None = Field(
        default=None, description="Restrict to these merchant_ids; None = any approved merchant"
    )
    created_at: datetime | None = None


class VolumeTier(_In):
    min_items: int = Field(ge=1)
    percent_off: Decimal = Field(ge=0, le=100)


class MerchantPolicy(_In):
    """Per-merchant shipping/discount terms. Supplied by Person 2."""

    merchant_id: str
    name: str
    shipping_fee: Decimal = Field(ge=0, description="Flat delivery fee per order")
    free_shipping_threshold: Decimal | None = Field(
        default=None, description="Order value (after discounts) at which delivery is free"
    )
    volume_discounts: list[VolumeTier] = Field(default_factory=list)
    delivers_to: list[str] | None = Field(
        default=None, description="Pickup points served; None = all"
    )
    approved: bool = Field(default=True, description="On the approved merchant list")
    is_simulated: bool = False


class MerchantOffer(_In):
    """A purchasable offer for one book at one merchant. Supplied by Person 2."""

    merchant_id: str
    book_id: str
    price: Decimal = Field(ge=0)
    in_stock: bool = True
    stock_qty: int | None = Field(default=None, description="None = unknown / plenty")
    delivery_days: int = Field(ge=0, description="Merchant's estimated days from order to delivery")
    fetched_at: datetime | None = None
    is_simulated: bool = False


class EngineConfig(_In):
    """Tunable business rules. Defaults are sensible for the demo."""

    cost_split: Literal["equal", "proportional"] = Field(
        default="equal",
        description="How a group's shipping fee is shared: equally, or by each line's value",
    )
    min_saving_per_member: Decimal = Field(
        default=Decimal("0.01"),
        description="Every group member must save at least this vs buying alone",
    )
    buy_now_marginal_threshold: Decimal = Field(
        default=Decimal("0.50"),
        description="Recommend buying when expected extra saving per person from waiting is below this",
    )
    order_processing_days: int = Field(
        default=1, ge=0, description="Days reserved for approvals + payment before the order is placed"
    )
    deadline_warning_days: int = Field(
        default=1, ge=0, description="Recommend buying when this many days or fewer remain to order"
    )
    low_stock_threshold: int = Field(default=2, ge=0)
    price_drop_percent: Decimal = Field(default=Decimal("5"))
    expected_arrivals_per_day: float = Field(
        default=0.5, ge=0, description="Expected new compatible requests per day (for wait-vs-buy)"
    )
    typical_book_price: Decimal = Field(default=Decimal("25.00"))
    max_group_size: int = Field(default=8, ge=2)
    exact_solver_max_requests: int = Field(
        default=12, ge=2, description="Use the exact partition solver up to this many requests per pickup point"
    )


# --------------------------------------------------------------------------- outputs


class Action(str, Enum):
    BUY_NOW = "BUY_NOW"
    WAIT = "WAIT"


class ReasonCode(str, Enum):
    FREE_SHIPPING_UNLOCKED = "FREE_SHIPPING_UNLOCKED"
    LOW_MARGINAL_VALUE = "LOW_MARGINAL_VALUE"
    DEADLINE_APPROACHING = "DEADLINE_APPROACHING"
    LOW_STOCK = "LOW_STOCK"
    PRICE_DROP = "PRICE_DROP"
    WAITING_LIKELY_PAYS_OFF = "WAITING_LIKELY_PAYS_OFF"
    MAX_GROUP_SIZE = "MAX_GROUP_SIZE"


class Recommendation(_Out):
    action: Action
    reasons: list[ReasonCode]
    expected_extra_saving_per_person: Decimal
    probability_more_buyers: float
    order_by: date = Field(description="Latest date the order can be placed and still meet every deadline")
    days_left_to_order: int
    explanation: str


class ParticipantLine(_Out):
    request_id: str
    user_id: str
    book_id: str
    title: str | None
    quantity: int
    unit_price: Decimal
    item_subtotal: Decimal
    discount: Decimal
    shipping_share: Decimal
    total: Decimal = Field(description="What this participant pays in the group")
    individual_best_total: Decimal
    individual_best_merchant: str
    individual_meets_deadline: bool
    savings: Decimal
    within_budget: bool
    meets_min_savings: bool
    deadline: date


class GroupProposal(_Out):
    group_id: str
    merchant_id: str
    merchant_name: str
    pickup_point: str
    participants: list[ParticipantLine]
    item_count: int
    subtotal: Decimal
    discount_total: Decimal
    shipping_fee: Decimal
    total: Decimal
    individual_total: Decimal
    total_savings: Decimal
    free_shipping_unlocked: bool
    amount_to_free_shipping: Decimal | None
    expected_delivery: date
    recommendation: Recommendation
    reasoning: list[str]
    is_simulated: bool

    @property
    def request_ids(self) -> list[str]:
        return [p.request_id for p in self.participants]

    @property
    def user_ids(self) -> list[str]:
        return [p.user_id for p in self.participants]


class SoloStatus(str, Enum):
    WAITING_FOR_GROUP = "WAITING_FOR_GROUP"
    BUY_SOLO_RECOMMENDED = "BUY_SOLO_RECOMMENDED"
    DEADLINE_UNREACHABLE = "DEADLINE_UNREACHABLE"
    UNSERVABLE = "UNSERVABLE"


class SoloPlan(_Out):
    """What to do with a request that is not (yet) in any group."""

    request_id: str
    user_id: str
    status: SoloStatus
    best_merchant: str | None
    best_total: Decimal | None
    best_expected_delivery: date | None
    potential_group_saving: Decimal
    order_by: date | None
    reasoning: list[str]


class EventType(str, Enum):
    GROUP_FORMED = "GROUP_FORMED"
    MEMBERS_JOINED = "MEMBERS_JOINED"
    MEMBERS_LEFT = "MEMBERS_LEFT"
    GROUPS_MERGED = "GROUPS_MERGED"
    GROUP_SPLIT = "GROUP_SPLIT"
    GROUP_RESTRUCTURED = "GROUP_RESTRUCTURED"
    GROUP_DISSOLVED = "GROUP_DISSOLVED"
    FREE_SHIPPING_UNLOCKED = "FREE_SHIPPING_UNLOCKED"
    BUY_NOW_RECOMMENDED = "BUY_NOW_RECOMMENDED"
    PRICE_DROP = "PRICE_DROP"
    SOLO_BUY_RECOMMENDED = "SOLO_BUY_RECOMMENDED"
    DEADLINE_UNREACHABLE = "DEADLINE_UNREACHABLE"
    REQUEST_UNSERVABLE = "REQUEST_UNSERVABLE"


class EngineEvent(_Out):
    """A proactive notification for Person 1 to send (Person 4 stores/dedupes it)."""

    event_type: EventType
    dedupe_key: str = Field(description="Same key = same notification; send once")
    group_id: str | None
    request_ids: list[str]
    user_ids: list[str]
    message: str = Field(description="Ready-to-send text; the bot may rephrase it with the LLM")
    data: dict[str, Any] = Field(default_factory=dict)
    needs_user_decision: bool = False


class EngineResult(_Out):
    evaluated_at: datetime
    proposals: list[GroupProposal]
    solo_plans: list[SoloPlan]
    events: list[EngineEvent]
    total_savings: Decimal
    uses_simulated_data: bool

    def proposal_for_request(self, request_id: str) -> GroupProposal | None:
        for p in self.proposals:
            if request_id in p.request_ids:
                return p
        return None

    def solo_plan_for_request(self, request_id: str) -> SoloPlan | None:
        for s in self.solo_plans:
            if s.request_id == request_id:
                return s
        return None


class EvaluateInput(_In):
    """Request body for the HTTP API (and a convenient bundle for the backend)."""

    requests: list[BookRequest]
    offers: list[MerchantOffer]
    merchants: list[MerchantPolicy]
    now: datetime | None = None
    previous: EngineResult | None = None
    previous_offers: list[MerchantOffer] | None = None
    config: EngineConfig | None = None

    @field_validator("requests")
    @classmethod
    def _unique_ids(cls, v: list[BookRequest]) -> list[BookRequest]:
        ids = [r.request_id for r in v]
        if len(ids) != len(set(ids)):
            raise ValueError("request_id values must be unique")
        return v
