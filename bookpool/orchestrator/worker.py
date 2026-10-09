"""Local integration worker for live-priced, simulated group purchases.

Run from the repository root: backend/.venv/bin/python -m bookpool.orchestrator.worker
"""

import asyncio
import hashlib
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bookpool-decision-engine"))
sys.path.insert(0, str(ROOT / "bookpool" / "bot"))
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings
from app.payments import ProviderError, ReapSandbox
from bookpool_bot.merchant_search import MerchantSearchClient
from bookpool_bot.models import BookFormat, BookRequestInput
from decision_engine import DecisionEngine
from decision_engine.models import (
    BookRequest as EngineRequest,
)
from decision_engine.models import (
    MerchantOffer as EngineOffer,
)
from decision_engine.models import (
    MerchantPolicy,
)

from bookpool.orchestrator.reap_catalog import ReapCatalog

log = logging.getLogger(__name__)
SGT = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ShippingPolicy:
    merchant_id: str
    display_name: str
    fee_minor: int
    free_above_minor: int
    working_days: int


# Current published Singapore domestic rates. Quote at the merchant checkout
# remains authoritative; do not call these estimates a completed merchant order.
POLICIES = {
    "Kinokuniya": ShippingPolicy("kinokuniya.com.sg", "Kinokuniya", 480, 5000, 7),
    "POPULAR": ShippingPolicy("popular.com.sg", "POPULAR", 490, 9000, 5),
}


def add_working_days(start: date, count: int) -> date:
    day = start
    while count:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count -= 1
    return day


def shipping_minor(subtotal: int, policy: ShippingPolicy) -> int:
    return 0 if subtotal > policy.free_above_minor else policy.fee_minor


def as_minor(amount: Decimal) -> int:
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError("Decision engine returned fractional cents")
    return int(cents)


class GroupWorker:
    """One-worker bridge. Backend transactions enforce quote and payment rules."""

    def __init__(
        self,
        backend_url: str,
        service_key: str,
        catalog: MerchantSearchClient | None = None,
        client: httpx.AsyncClient | None = None,
        reap_catalog: ReapCatalog | None = None,
        shipping_address: dict | None = None,
        purchaser_email: str | None = None,
    ):
        self.client = client or httpx.AsyncClient(
            base_url=backend_url.rstrip("/"),
            headers={"Authorization": f"Bearer {service_key}"},
            timeout=12,
        )
        self.owns_client = client is None
        self.catalog = catalog or MerchantSearchClient()
        self.owns_catalog = catalog is None
        self.engine = DecisionEngine()
        self.last_lookup: dict[str, datetime] = {}
        self.last_reconcile: dict[str, datetime] = {}
        self.reap_catalog = reap_catalog
        self.shipping_address = shipping_address
        self.purchaser_email = purchaser_email
        self.payment_mode = "mock"

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()
        if self.owns_catalog:
            await self.catalog.close()

    async def api(self, method: str, path: str, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def run_once(self) -> None:
        health = await self.api("GET", "/health")
        self.payment_mode = health["payment_mode"]
        if self.payment_mode == "reap_sandbox" and (
            self.reap_catalog is None
            or self.shipping_address is None
            or not self.purchaser_email
        ):
            log.warning(
                "Reap sandbox worker needs catalog credentials, shipping contact and purchaser email"
            )
            return
        await self.api("POST", "/v1/maintenance/expire")
        now = datetime.now(timezone.utc)
        requests = [
            item
            for item in await self.api(
                "GET", "/v1/requests", params={"status": "OPEN", "limit": 500}
            )
            if item.get("group_id") is None
        ]
        offers_by_request: dict[str, list[dict]] = {}
        for request in requests:
            try:
                offers_by_request[request["id"]] = await self._offers_for(request, now)
            except (httpx.HTTPError, ProviderError, ValueError, KeyError):
                log.exception("offer_refresh_failed request_id=%s", request["id"])
                offers_by_request[request["id"]] = []
        if len({item["user_id"] for item in requests}) >= 2:
            await self._propose(requests, offers_by_request, now)
        if self.payment_mode == "reap_sandbox":
            await self._quote_unquoted()
        await self._execute_ready()

    async def _offers_for(self, request: dict, now: datetime) -> list[dict]:
        raw = await self.api(
            "GET", "/v1/offers", params={"request_id": request["id"], "limit": 500}
        )
        fresh = [
            offer
            for offer in raw
            if offer["source"]
            == ("reap" if self.payment_mode == "reap_sandbox" else "merchant")
            and offer["available"]
            and datetime.fromisoformat(offer["expires_at"]) > now
            and datetime.fromisoformat(offer["delivery_at"])
            <= datetime.fromisoformat(request["latest_delivery_at"])
        ]
        known = {offer["merchant"] for offer in fresh}
        last = self.last_lookup.get(request["id"])
        if len(known) < len(POLICIES) and (
            last is None or now - last >= timedelta(minutes=5)
        ):
            self.last_lookup[request["id"]] = now
            if self.payment_mode == "reap_sandbox":
                matches = await self.reap_catalog.search(
                    request["title"], request.get("isbn"), request.get("edition")
                )
            else:
                desired_format = request.get("edition")
                search_request = BookRequestInput(
                    title=request["title"],
                    isbn=request.get("isbn"),
                    format=BookFormat(desired_format)
                    if desired_format in {"paperback", "hardcover"}
                    else BookFormat.ANY,
                    latest_delivery_date=datetime.fromisoformat(
                        request["latest_delivery_at"]
                    ).date(),
                    maximum_budget_minor=request["budget_minor"],
                )
                matches = (await self.catalog.search(search_request)).matches
            cheapest = {}
            rejected_delivery_dates = []
            for match in matches:
                policy = (
                    next(
                        (
                            p
                            for p in POLICIES.values()
                            if p.merchant_id == match.merchant_id
                        ),
                        None,
                    )
                    if self.payment_mode == "reap_sandbox"
                    else POLICIES.get(match.merchant)
                )
                if policy and policy.merchant_id not in known:
                    previous = cheapest.get(policy.merchant_id)
                    if previous is None or match.price_minor < previous.price_minor:
                        cheapest[policy.merchant_id] = match
            for merchant_id, match in cheapest.items():
                policy = next(
                    p for p in POLICIES.values() if p.merchant_id == merchant_id
                )
                delivery_day = add_working_days(
                    now.astimezone(SGT).date(), policy.working_days
                )
                delivery_at = datetime.combine(delivery_day, time(18), tzinfo=SGT)
                if delivery_at > datetime.fromisoformat(request["latest_delivery_at"]):
                    rejected_delivery_dates.append(delivery_at)
                    continue
                body = {
                    "request_id": request["id"],
                    "merchant": merchant_id,
                    "variant_id": match.variant_id
                    if self.payment_mode == "reap_sandbox"
                    else "catalog:" + match.url.rsplit("/", 1)[-1],
                    "unit_price_minor": match.price_minor,
                    "individual_total_minor": match.price_minor
                    + shipping_minor(match.price_minor, policy),
                    "available": True,
                    "delivery_at": delivery_at.isoformat(),
                    "expires_at": (now + timedelta(minutes=30)).isoformat(),
                    "source": "reap"
                    if self.payment_mode == "reap_sandbox"
                    else "merchant",
                }
                fresh.append(await self.api("POST", "/v1/offers", json=body))
            if not fresh:
                status = {"code": "NO_MATCH"}
                if rejected_delivery_dates:
                    status = {
                        "code": "DEADLINE_TOO_SOON",
                        "earliest_delivery_at": min(
                            rejected_delivery_dates
                        ).isoformat(),
                    }
                await self.api(
                    "POST", f"/v1/requests/{request['id']}/search-status", json=status
                )
        newest: dict[str, dict] = {}
        for offer in fresh:
            current = newest.get(offer["merchant"])
            if current is None or offer["created_at"] > current["created_at"]:
                newest[offer["merchant"]] = offer
        return list(newest.values())

    async def _propose(
        self,
        requests: list[dict],
        offers_by_request: dict[str, list[dict]],
        now: datetime,
    ) -> None:
        engine_requests = [
            EngineRequest(
                request_id=item["id"],
                user_id=item["user_id"],
                book_id=item["id"],
                title=item["title"],
                quantity=item["quantity"],
                deadline=datetime.fromisoformat(item["latest_delivery_at"])
                .astimezone(SGT)
                .date(),
                max_budget=Decimal(item["budget_minor"]) / 100,
                min_savings_to_wait=Decimal(item["min_savings_minor"]) / 100,
                pickup_point=item["pickup_location"],
            )
            for item in requests
        ]
        all_offers = [offer for items in offers_by_request.values() for offer in items]
        engine_offers = [
            EngineOffer(
                merchant_id=offer["merchant"],
                book_id=offer["request_id"],
                price=Decimal(offer["unit_price_minor"]) / 100,
                delivery_days=max(
                    1,
                    (
                        datetime.fromisoformat(offer["delivery_at"])
                        .astimezone(SGT)
                        .date()
                        - now.astimezone(SGT).date()
                    ).days,
                ),
                fetched_at=datetime.fromisoformat(offer["created_at"]),
                is_simulated=False,
            )
            for offer in all_offers
        ]
        policies = [
            MerchantPolicy(
                merchant_id=policy.merchant_id,
                name=policy.display_name,
                shipping_fee=Decimal(policy.fee_minor) / 100,
                free_shipping_threshold=Decimal(policy.free_above_minor + 1) / 100,
                approved=True,
                is_simulated=False,
            )
            for policy in POLICIES.values()
        ]
        result = self.engine.evaluate(
            engine_requests, engine_offers, policies, now=now.astimezone(SGT)
        )
        offer_ids = {(o["request_id"], o["merchant"]): o["id"] for o in all_offers}
        for proposal in result.proposals:
            if len({p.user_id for p in proposal.participants}) < 2:
                continue
            selected = [
                offer_ids[(p.request_id, proposal.merchant_id)]
                for p in proposal.participants
            ]
            expiry = min(
                [now + timedelta(minutes=20)]
                + [
                    datetime.fromisoformat(o["expires_at"])
                    for o in all_offers
                    if o["id"] in selected
                ]
            )
            if expiry <= now:
                continue
            reference = (
                "live-"
                + hashlib.sha256(
                    (proposal.group_id + ":" + ":".join(sorted(selected))).encode()
                ).hexdigest()[:28]
            )
            body = {
                "client_reference": reference,
                "merchant": proposal.merchant_id,
                "purchaser_id": proposal.participants[0].user_id,
                "total_minor": as_minor(proposal.total),
                "expires_at": expiry.isoformat(),
                "delivery_at": datetime.combine(
                    proposal.expected_delivery, time(18), tzinfo=SGT
                ).isoformat(),
                "pickup_location": proposal.pickup_point,
                "reason": proposal.recommendation.explanation[:1000],
                "allocations": [
                    {
                        "request_id": line.request_id,
                        "offer_id": offer_ids[(line.request_id, proposal.merchant_id)],
                        "amount_minor": as_minor(line.total),
                    }
                    for line in proposal.participants
                ],
            }
            if self.payment_mode == "reap_sandbox":
                body["shipping_address"] = self.shipping_address
            try:
                group = await self.api("POST", "/v1/groups", json=body)
                log.info(
                    "group_proposed id=%s members=%s",
                    group["id"],
                    len(proposal.participants),
                )
            except httpx.HTTPStatusError as exc:
                log.warning(
                    "group_proposal_rejected status=%s body=%s",
                    exc.response.status_code,
                    exc.response.text[:300],
                )

    async def _quote_unquoted(self) -> None:
        for group in await self.api("GET", "/v1/groups", params={"limit": 500}):
            if group["state"] != "PROPOSED" or group.get("quote_id"):
                continue
            try:
                quoted = await self.api(
                    "POST",
                    f"/v1/groups/{group['id']}/reap-quote",
                    json={"version": group["version"], "email": self.purchaser_email},
                )
                log.info(
                    "reap_quote_ready group_id=%s version=%s",
                    group["id"],
                    quoted["group"]["version"],
                )
            except httpx.HTTPStatusError as exc:
                log.warning(
                    "reap_quote_failed group_id=%s status=%s",
                    group["id"],
                    exc.response.status_code,
                )
                if exc.response.status_code == 422:
                    await self.api(
                        "POST",
                        f"/v1/groups/{group['id']}/cancel",
                        json={
                            "reason": "Reap quote could not satisfy group constraints"
                        },
                    )

    async def _execute_ready(self) -> None:
        for group in await self.api("GET", "/v1/groups", params={"limit": 500}):
            if group["state"] == "READY":
                try:
                    order = await self.api(
                        "POST",
                        f"/v1/groups/{group['id']}/execute",
                        json={"version": group["version"]},
                    )
                    log.info("order state=%s id=%s", order["state"], order["id"])
                except httpx.HTTPStatusError:
                    log.exception("group_execution_failed group_id=%s", group["id"])
            elif group["state"] in {
                "CHECKOUT_PENDING",
                "PAYMENT_UNKNOWN",
                "PAYMENT_PENDING",
                "AWAITING_PAYMENT_APPROVAL",
            }:
                order = group.get("order")
                if (
                    not order
                    or order.get("error_code") == "MANUAL_RECONCILIATION_REQUIRED"
                ):
                    continue
                previous = self.last_reconcile.get(order["id"])
                now = datetime.now(timezone.utc)
                if previous and now - previous < timedelta(seconds=30):
                    continue
                self.last_reconcile[order["id"]] = now
                try:
                    await self.api("POST", f"/v1/orders/{order['id']}/reconcile")
                except httpx.HTTPStatusError:
                    log.exception(
                        "order_reconciliation_failed order_id=%s", order["id"]
                    )


async def main() -> None:
    env_file = ROOT / "backend" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                os.environ.setdefault(name.strip(), value.strip().strip("\"'"))
    key_file = ROOT / "backend" / "data" / "service.key"
    key = os.getenv("BOOKPOOL_SERVICE_API_KEY") or (
        key_file.read_text().strip() if key_file.exists() else ""
    )
    if not key:
        raise SystemExit("Start the backend first or set BOOKPOOL_SERVICE_API_KEY")
    settings = Settings.from_env()
    contact = os.getenv("BOOKPOOL_SHIPPING_NAME", "Book Pool").split(" ", 1)
    shipping_address = {
        "firstName": contact[0],
        "lastName": contact[1] if len(contact) > 1 else "Tester",
        "phone": os.getenv("BOOKPOOL_SHIPPING_PHONE", "+6591234567"),
        "addressLine1": "41 Students Walk",
        "city": "Singapore",
        "postalCode": "639549",
        "country": "SG",
    }
    worker = GroupWorker(
        os.getenv("BACKEND_URL", "http://127.0.0.1:8000"),
        key,
        reap_catalog=ReapCatalog(ReapSandbox(settings))
        if settings.reap_api_key
        else None,
        shipping_address=shipping_address,
        purchaser_email=os.getenv(
            "BOOKPOOL_PURCHASER_EMAIL", "bookpool.test@example.com"
        ),
    )
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        while True:
            try:
                await worker.run_once()
            except (httpx.HTTPError, ProviderError, ValueError, KeyError):
                log.exception("group_worker_cycle_failed")
            await asyncio.sleep(10)
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
