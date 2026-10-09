"""FastAPI adapter for BookPool merchant intelligence."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .allowlist import OFFICIAL_BOOK_MERCHANTS
from .models import BookQuery
from .reap import ReapApiError
from .runtime import MerchantServiceConfigurationError, create_runtime


class ShippingAddressRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    first_name: str = Field(..., alias="firstName")
    last_name: str = Field(..., alias="lastName")
    phone: str
    address_line1: str = Field(..., alias="addressLine1")
    city: str
    country: str
    address_line2: Optional[str] = Field(None, alias="addressLine2")
    region: Optional[str] = None
    postal_code: Optional[str] = Field(None, alias="postalCode")

class CompareOffersRequest(BaseModel):
    title: str = ""
    author: Optional[str] = None
    isbn: Optional[str] = None
    edition: Optional[str] = None
    format: Optional[str] = None
    quantity: int = Field(1, ge=1, le=20)
    destination: str = "Singapore"
    required_by: Optional[date] = None
    max_budget_sgd: Optional[Decimal] = Field(None, ge=0)
    include_out_of_stock: bool = True
    buyer_email: Optional[str] = None
    shipping_address: Optional[ShippingAddressRequest] = None
    country: str = Field("SG", min_length=2, max_length=2)
    currency: str = Field("SGD", min_length=3, max_length=3)
    limit: int = Field(8, ge=1, le=10)


class ReapQuoteItemRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    offer_id: str = Field(..., alias="offerId", min_length=1)
    quantity: int = Field(1, ge=1, le=20)

class ReapCartQuoteRequest(BaseModel):
    items: List[ReapQuoteItemRequest] = Field(..., min_items=1, max_items=20)
    buyer_email: str
    shipping_address: ShippingAddressRequest
    offer_code: Optional[str] = None


router = APIRouter(prefix="/merchant", tags=["merchant-intelligence"])


def _http_error_for(exc: Exception) -> HTTPException:
    if isinstance(exc, MerchantServiceConfigurationError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ReapApiError):
        # Reap's message is safe contextual feedback; never include the API key
        # or raw request/response body in our own API response.
        return HTTPException(
            status_code=502,
            detail={"code": exc.code, "message": exc.message},
        )
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="Merchant intelligence request failed")


@router.get("/health")
def health() -> Dict[str, Any]:
    try:
        runtime = create_runtime()
        return {
            "status": "ok",
            "mode": runtime.mode,
            "approved_book_merchants": list(OFFICIAL_BOOK_MERCHANTS),
        }
    except MerchantServiceConfigurationError as exc:
        return {
            "status": "not_configured",
            "mode": "reap_sandbox",
            "detail": str(exc),
            "approved_book_merchants": list(OFFICIAL_BOOK_MERCHANTS),
        }


@router.get("/merchants")
def merchants() -> Dict[str, Any]:
    try:
        runtime = create_runtime()
        return {"mode": runtime.mode, "merchants": runtime.merchant_catalog()}
    except Exception as exc:
        raise _http_error_for(exc)


@router.post("/offers")
def compare_offers(request: CompareOffersRequest) -> Dict[str, Any]:
    try:
        query = BookQuery(
            title=request.title,
            author=request.author,
            isbn=request.isbn,
            edition=request.edition,
            format=request.format,
            quantity=request.quantity,
            destination=request.destination,
            required_by=request.required_by,
            max_budget_sgd=request.max_budget_sgd,
            include_out_of_stock=request.include_out_of_stock,
        )
        runtime = create_runtime()
        shipping_address = (
            request.shipping_address.model_dump(by_alias=True, exclude_none=True)
            if request.shipping_address
            else None
        )
        return runtime.compare_offers(
            query,
            buyer_email=request.buyer_email,
            shipping_address=shipping_address,
            country=request.country.upper(),
            currency=request.currency.upper(),
            limit=request.limit,
        )
    except Exception as exc:
        raise _http_error_for(exc)


@router.post("/cart-quote")
def quote_cart(request: ReapCartQuoteRequest) -> Dict[str, Any]:
    try:
        runtime = create_runtime()
        return runtime.quote_reap_cart(
            [item.model_dump(by_alias=True) for item in request.items],
            request.buyer_email,
            request.shipping_address.model_dump(by_alias=True, exclude_none=True),
            request.offer_code,
        )
    except Exception as exc:
        raise _http_error_for(exc)


def create_app() -> FastAPI:
    app = FastAPI(
        title="BookPool Merchant Intelligence",
        version="0.1.0",
        description="Approved-book merchant discovery and Reap sandbox quote comparison.",
    )
    app.include_router(router)
    return app
