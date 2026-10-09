from datetime import datetime, timezone
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

Minor = Annotated[int, Field(strict=True, ge=0, le=100_000_000)]
PositiveMinor = Annotated[int, Field(strict=True, gt=0, le=100_000_000)]
Id = Annotated[str, Field(min_length=1, max_length=200)]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UserIn(Payload):
    telegram_id: Annotated[str, Field(min_length=1, max_length=40)]
    display_name: Annotated[str, Field(min_length=1, max_length=100)]


class RequestIn(Payload):
    user_id: Id
    title: Annotated[str, Field(min_length=1, max_length=300)]
    isbn: Annotated[str, Field(pattern=r"^(?:\d{13}|\d{9}[\dX])$")] | None = None
    edition: Annotated[str, Field(max_length=100)] | None = None
    quantity: Annotated[int, Field(strict=True, ge=1, le=20)] = 1
    budget_minor: PositiveMinor
    min_savings_minor: Minor = 0
    currency: Literal["SGD"] = "SGD"
    latest_delivery_at: datetime
    pickup_location: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("latest_delivery_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("Use an ISO timestamp with timezone, e.g. +08:00")
        return value.astimezone(timezone.utc)


class OfferIn(Payload):
    request_id: Id
    merchant: Annotated[str, Field(min_length=1, max_length=100)]
    variant_id: Id
    unit_price_minor: PositiveMinor
    individual_total_minor: PositiveMinor
    currency: Literal["SGD"] = "SGD"
    available: bool = True
    delivery_at: datetime
    expires_at: datetime
    source: Literal["mock", "merchant", "reap"] = "mock"
    _aware = field_validator("delivery_at", "expires_at")(RequestIn.aware.__func__)


class Allocation(Payload):
    request_id: Id
    offer_id: Id
    amount_minor: PositiveMinor


class Address(Payload):
    firstName: Annotated[str, Field(min_length=1, max_length=100)]
    lastName: Annotated[str, Field(min_length=1, max_length=100)]
    phone: Annotated[str, Field(min_length=1, max_length=40)]
    addressLine1: Annotated[str, Field(min_length=1, max_length=300)]
    addressLine2: Annotated[str, Field(max_length=300)] | None = None
    city: str = "Singapore"
    postalCode: Annotated[str, Field(pattern=r"^\d{6}$")]
    country: Literal["SG"] = "SG"


class ProposalIn(Payload):
    client_reference: Annotated[str, Field(min_length=1, max_length=100)]
    merchant: Annotated[str, Field(min_length=1, max_length=100)]
    purchaser_id: Id
    total_minor: PositiveMinor
    currency: Literal["SGD"] = "SGD"
    expires_at: datetime
    delivery_at: datetime
    pickup_location: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    allocations: Annotated[list[Allocation], Field(min_length=2, max_length=20)]
    shipping_address: Address | None = None
    _aware = field_validator("delivery_at", "expires_at")(RequestIn.aware.__func__)


class VersionIn(Payload):
    version: Annotated[int, Field(strict=True, ge=1)]


class AuthorizeIn(VersionIn):
    outcome: Literal["success", "decline"] = "success"


class ExecuteIn(VersionIn):
    outcome: Literal["success", "decline", "timeout"] = "success"


class QuoteIn(VersionIn):
    email: Annotated[str, Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=254)]
    offer_code: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    shipping_option_id: Id | None = None


class EnrollmentIn(Payload):
    # Provider payload fields vary by enrollment source; never accept raw card details here.
    email: Annotated[str, Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", max_length=254)]
    return_url: Annotated[str, Field(min_length=1, max_length=2000)]


class AttachEnrollmentIn(Payload):
    enrollment_id: Id


class ReasonIn(Payload):
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
