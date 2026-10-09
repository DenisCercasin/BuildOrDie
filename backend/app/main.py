import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .db import BookRequest, Database, Event, Group, Offer, Order, Participant, PaymentTransaction, User, uid
from .domain import (emit, ensure_ready, fail, fingerprint, future, group_view, invalidate,
                     model_dict, open_group, order_view, participants, refresh_ready,
                     release_requests, row, timestamp, validate_proposal, version,
                     individual_baseline, journal, void_holds)
from .payments import ProviderError, ReapSandbox, money_minor
from .schemas import (AttachEnrollmentIn, AuthorizeIn, EnrollmentIn, ExecuteIn, OfferIn,
                      ProposalIn, QuoteIn, ReasonIn, RequestIn, SearchStatusIn, UserIn, VersionIn)


def allocate(total, weights):
    """Largest remainder allocation: deterministic cents, exact reconciliation."""
    denominator = sum(weights)
    values = [total * w // denominator for w in weights]
    remainders = [total * w % denominator for w in weights]
    for i in sorted(range(len(weights)), key=lambda i: (-remainders[i], i))[:total - sum(values)]:
        values[i] += 1
    return values


def create_app(settings=None, provider=None):
    settings = settings or Settings.from_env()
    settings.validate()
    db = Database(settings.database_url)
    reap = provider or ReapSandbox(settings)
    api = FastAPI(title="BookPool — Person 4 Backend", version="1.0.0",
                  description="SGD group commitments and sandbox order orchestration. All payments are simulated.")
    api.state.db = db
    api.state.settings = settings
    api.state.provider = reap

    def auth(authorization: str = Header(default="")):
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not token:
            fail("AUTH_REQUIRED", "Provide a Bearer token", 401)
        if secrets.compare_digest(token, settings.service_api_key):
            return {"service": True, "user_id": None}
        with db.transaction() as s:
            user = s.scalar(select(User).where(User.token_hash == hashlib.sha256(token.encode()).hexdigest()))
            if not user:
                fail("AUTH_INVALID", "Invalid Bearer token", 401)
            return {"service": False, "user_id": user.id}

    def service(actor=Depends(auth)):
        if not actor["service"]:
            fail("SERVICE_ONLY", "Only trusted team services may call this endpoint", 403)
        return actor

    def owns(actor, user_id):
        if not actor["service"] and actor["user_id"] != user_id:
            fail("FORBIDDEN", "You cannot act for another participant", 403)

    def can_view(s, actor, group):
        if not actor["service"] and actor["user_id"] not in {p.user_id for p in participants(s, group.id)}:
            fail("FORBIDDEN", "This group belongs to other participants", 403)

    def require_reap():
        if settings.payment_mode != "reap_sandbox":
            fail("REAP_NOT_ENABLED", "Enable reap_sandbox after supplying the sandbox key")

    def provider_failure(error):
        fail(error.code, "Reap could not confirm the request. Check configuration or reconcile before retrying.", 502)

    @api.get("/health", tags=["Operations"])
    def health():
        with db.transaction() as s:
            s.execute(text("SELECT 1"))
        return {"status": "ok", "payment_mode": settings.payment_mode, "sandbox_only": True,
                "public_base_url": settings.public_base_url,
                "payment_return_url": settings.hosted_return_url}

    @api.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home():
        return """<!doctype html><html><head><meta charset="utf-8"><title>BookPool Backend</title>
        <style>body{font:18px system-ui;background:#f5f3ed;color:#193b32;max-width:750px;margin:70px auto;padding:24px}
        h1{font-size:48px}a{color:#176756}p{line-height:1.6}small{color:#6a756f}</style></head><body>
        <small>BOOKPOOL · PERSON 4</small><h1>From approval to order.</h1>
        <p>Backend for group commitments, budget checks and simulated payment orchestration.</p>
        <p><a href="/docs">Open interactive API documentation →</a></p>
        <p><a href="/health">Check service health</a></p><small>Sandbox only. No real funds or deliveries.</small>
        </body></html>"""

    @api.post("/v1/users", tags=["Users"])
    def create_user(body: UserIn, _=Depends(service)):
        with db.transaction(write=True) as s:
            existing = s.scalar(select(User).where(User.telegram_id == body.telegram_id))
            if existing:
                return {"id": existing.id, "display_name": existing.display_name, "access_token": None,
                        "message": "Existing user; use the bot service key or rotate their token"}
            token = secrets.token_urlsafe(32)
            user = User(**body.model_dump(), token_hash=hashlib.sha256(token.encode()).hexdigest())
            s.add(user)
            s.flush()
            return {"id": user.id, "display_name": user.display_name, "access_token": token}

    @api.post("/v1/users/{user_id}/token", tags=["Users"])
    def rotate_token(user_id: str, _=Depends(service)):
        with db.transaction(write=True) as s:
            user = row(s, User, user_id, lock=True)
            token = secrets.token_urlsafe(32)
            user.token_hash = hashlib.sha256(token.encode()).hexdigest()
            return {"id": user.id, "access_token": token}

    @api.post("/v1/requests", tags=["Requests"])
    def create_request(body: RequestIn, actor=Depends(auth)):
        owns(actor, body.user_id)
        if not future(body.latest_delivery_at):
            fail("DELIVERY_DEADLINE", "Delivery deadline must be in the future", 422)
        with db.transaction(write=True) as s:
            row(s, User, body.user_id)
            data = body.model_dump()
            data["latest_delivery_at"] = body.latest_delivery_at.isoformat()
            req = BookRequest(**data)
            s.add(req)
            s.flush()
            emit(s, "REQUEST_CREATED", request_id=req.id, user_id=req.user_id)
            return model_dict(req)

    @api.get("/v1/requests", tags=["Requests"])
    def list_requests(status: str | None = None, limit: int = Query(100, ge=1, le=500), actor=Depends(auth)):
        with db.transaction() as s:
            stmt = select(BookRequest).order_by(BookRequest.created_at).limit(limit)
            if not actor["service"]:
                stmt = stmt.where(BookRequest.user_id == actor["user_id"])
            if status:
                stmt = stmt.where(BookRequest.status == status)
            return [model_dict(r) for r in s.scalars(stmt)]

    @api.post("/v1/requests/{request_id}/cancel", tags=["Requests"])
    def cancel_request(request_id: str, actor=Depends(auth)):
        with db.transaction(write=True) as s:
            req = row(s, BookRequest, request_id, lock=True)
            owns(actor, req.user_id)
            if req.group_id or req.status == "ORDERED":
                fail("REQUEST_RESERVED", "Cancel the pending group before cancelling this request")
            req.status = "CANCELLED"
            emit(s, "REQUEST_CANCELLED", request_id=req.id)
            return model_dict(req)

    @api.post("/v1/offers", tags=["Merchant Intelligence"])
    def create_offer(body: OfferIn, _=Depends(service)):
        if body.merchant not in settings.allowed_merchants:
            fail("MERCHANT_NOT_ALLOWED", "Merchant is not approved", 422)
        if not future(body.expires_at):
            fail("OFFER_EXPIRED", "Provide a fresh offer", 422)
        with db.transaction(write=True) as s:
            req = row(s, BookRequest, body.request_id)
            if body.individual_total_minor < body.unit_price_minor * req.quantity:
                fail("INVALID_OFFER_TOTAL", "Individual total cannot be below its item subtotal", 422)
            data = body.model_dump()
            for key in ("delivery_at", "expires_at"):
                data[key] = getattr(body, key).isoformat()
            offer = Offer(**data)
            s.add(offer)
            s.flush()
            return model_dict(offer)

    @api.post("/v1/requests/{request_id}/search-status", tags=["Merchant Intelligence"])
    def report_search_status(request_id: str, body: SearchStatusIn, _=Depends(service)):
        with db.transaction(write=True) as s:
            req = row(s, BookRequest, request_id, lock=True)
            if req.status != "OPEN" or req.group_id:
                return {"changed": False}
            payload = {"request_id": req.id, "user_id": req.user_id,
                       "latest_delivery_at": req.latest_delivery_at,
                       **body.model_dump(mode="json")}
            previous = s.scalar(select(Event).where(
                Event.kind == "REQUEST_SEARCH_STATUS",
                Event.payload["request_id"].as_string() == req.id,
            ).order_by(Event.id.desc()).limit(1))
            if previous and previous.payload == payload:
                return {"changed": False}
            emit(s, "REQUEST_SEARCH_STATUS", **payload)
            return {"changed": True}

    @api.get("/v1/offers", tags=["Merchant Intelligence"])
    def list_offers(request_id: str | None = None, limit: int = Query(100, ge=1, le=500), _=Depends(service)):
        with db.transaction() as s:
            stmt = select(Offer).limit(limit)
            if request_id:
                stmt = stmt.where(Offer.request_id == request_id)
            return [model_dict(o) for o in s.scalars(stmt)]

    def write_proposal(s, body, group=None):
        found = validate_proposal(s, body, settings.allowed_merchants, group.id if group else None)
        if group:
            if group.state not in {"PROPOSED", "READY"}:
                fail("INVALID_STATE", "Cannot revise a group after checkout starts")
            void_holds(s, group)
            release_requests(s, group)
            for p in participants(s, group.id):
                s.delete(p)
            s.flush()
            group.version += 1
        else:
            group = Group(client_reference=body.client_reference)
            s.add(group)
        group.fingerprint = fingerprint(body.model_dump(mode="json"))
        for field in ("merchant", "purchaser_id", "total_minor", "currency", "pickup_location", "reason"):
            setattr(group, field, getattr(body, field))
        group.delivery_at = body.delivery_at.isoformat()
        group.expires_at = body.expires_at.isoformat()
        group.shipping_address = body.shipping_address.model_dump(exclude_none=True) if body.shipping_address else {}
        group.state = "PROPOSED"
        group.quote_id = None
        s.flush()
        for req, offer, a in found:
            req.group_id = group.id
            s.add(Participant(group_id=group.id, request_id=req.id, user_id=req.user_id,
                              offer_id=offer.id, amount_minor=a.amount_minor))
        s.flush()
        if settings.payment_mode == "mock":
            emit(s, "PROPOSAL_CREATED", group.id, version=group.version, reason=body.reason)
        return group_view(s, group)

    @api.post("/v1/groups", tags=["Groups"])
    def create_group(body: ProposalIn, _=Depends(service)):
        try:
            with db.transaction(write=True) as s:
                existing = s.scalar(select(Group).where(Group.client_reference == body.client_reference))
                if existing:
                    if existing.fingerprint != fingerprint(body.model_dump(mode="json")):
                        fail("IDEMPOTENCY_CONFLICT", "Client reference was already used for a different proposal")
                    return group_view(s, existing)
                return write_proposal(s, body)
        except IntegrityError:
            fail("CONCURRENT_CONFLICT", "A concurrent request reserved these records; reload and retry")

    @api.put("/v1/groups/{group_id}/proposal", tags=["Groups"])
    def revise_group(group_id: str, body: ProposalIn, expected_version: int = Query(ge=1), _=Depends(service)):
        with db.transaction(write=True) as s:
            group = row(s, Group, group_id, lock=True)
            version(group, expected_version)
            if body.client_reference != group.client_reference:
                fail("REFERENCE_MISMATCH", "Keep the group's client_reference", 422)
            return write_proposal(s, body, group)

    @api.get("/v1/groups", tags=["Groups"])
    def list_groups(limit: int = Query(100, ge=1, le=500), actor=Depends(auth)):
        with db.transaction() as s:
            stmt = select(Group).order_by(Group.created_at).limit(limit)
            if not actor["service"]:
                stmt = stmt.where(Group.id.in_(select(Participant.group_id).where(Participant.user_id == actor["user_id"])))
            return [group_view(s, g) for g in s.scalars(stmt)]

    @api.get("/v1/groups/{group_id}", tags=["Groups"])
    def get_group(group_id: str, actor=Depends(auth)):
        with db.transaction() as s:
            group = row(s, Group, group_id)
            can_view(s, actor, group)
            return group_view(s, group)

    def member_action(gid, user_id, body, actor, action):
        owns(actor, user_id)
        with db.transaction(write=True) as s:
            group = row(s, Group, gid, lock=True)
            version(group, body.version)
            open_group(group)
            ps = [p for p in participants(s, gid) if p.user_id == user_id]
            if not ps:
                fail("NOT_PARTICIPANT", "User is not in this group", 404)
            if action == "approve":
                if all(p.approved_version == group.version for p in ps):
                    return group_view(s, group)
                for p in ps:
                    p.approved_version = group.version
                emit(s, "PARTICIPANT_APPROVED", gid, user_id=user_id, version=group.version)
            elif action == "authorize":
                if not all(p.approved_version == group.version for p in ps):
                    fail("APPROVAL_REQUIRED", "Approve the current proposal before authorizing")
                for p in ps:
                    if body.outcome == "decline" and p.payment_state == "AUTHORIZED":
                        fail("ALREADY_AUTHORIZED", "An authorized contribution cannot be changed to declined")
                    journal(s, group, p, "AUTHORIZE" if body.outcome == "success" else "AUTHORIZATION_FAILED")
                    p.payment_state = "AUTHORIZED" if body.outcome == "success" else "DECLINED"
                emit(s, "MOCK_CONTRIBUTION_AUTHORIZED" if body.outcome == "success" else "MOCK_CONTRIBUTION_DECLINED",
                     gid, user_id=user_id, simulated=True)
            else:
                # Withdrawal changes group economics: require optimizer to rebuild the whole proposal.
                group.state = "CANCELLED"
                void_holds(s, group)
                release_requests(s, group)
                emit(s, "GROUP_CANCELLED", gid, reason="Participant withdrew", user_id=user_id)
                return group_view(s, group)
            refresh_ready(s, group, settings.payment_mode)
            return group_view(s, group)

    @api.post("/v1/groups/{group_id}/participants/{user_id}/approve", tags=["Commitments"])
    def approve(group_id: str, user_id: str, body: VersionIn, actor=Depends(auth)):
        return member_action(group_id, user_id, body, actor, "approve")

    @api.post("/v1/groups/{group_id}/participants/{user_id}/authorize", tags=["Commitments"])
    def authorize(group_id: str, user_id: str, body: AuthorizeIn, actor=Depends(auth)):
        return member_action(group_id, user_id, body, actor, "authorize")

    @api.post("/v1/groups/{group_id}/participants/{user_id}/withdraw", tags=["Commitments"])
    def withdraw(group_id: str, user_id: str, body: VersionIn, actor=Depends(auth)):
        return member_action(group_id, user_id, body, actor, "withdraw")

    @api.post("/v1/groups/{group_id}/cancel", tags=["Groups"])
    def cancel_group(group_id: str, body: ReasonIn, _=Depends(service)):
        with db.transaction(write=True) as s:
            group = row(s, Group, group_id, lock=True)
            if group.state == "CANCELLED":
                return group_view(s, group)
            if group.state not in {"PROPOSED", "READY", "EXPIRED", "FAILED"}:
                fail("CHECKOUT_STARTED", "Reconcile pending payment or request a refund after order confirmation")
            group.state = "CANCELLED"
            void_holds(s, group)
            release_requests(s, group)
            emit(s, "GROUP_CANCELLED", group.id, reason=body.reason)
            return group_view(s, group)

    @api.post("/v1/maintenance/expire", tags=["Operations"])
    def expire_groups(_=Depends(service)):
        with db.transaction(write=True) as s:
            groups = s.scalars(select(Group).where(Group.state.in_(["PROPOSED", "READY"])).with_for_update())
            expired = []
            for group in groups:
                if not future(group.expires_at):
                    group.state = "EXPIRED"
                    void_holds(s, group)
                    release_requests(s, group)
                    emit(s, "GROUP_EXPIRED", group.id)
                    expired.append(group.id)
            return {"expired_group_ids": expired}

    @api.post("/v1/users/{user_id}/enrollments", tags=["Reap Sandbox"])
    def enrollment(user_id: str, body: EnrollmentIn,
                   idempotency_key: str = Header(min_length=1, max_length=200), actor=Depends(auth)):
        require_reap()
        owns(actor, user_id)
        target = urlparse(body.return_url)
        if body.return_url != settings.hosted_return_url or target.username or target.password:
            fail("RETURN_URL_NOT_ALLOWED", "Return URL must match the configured payment return URL", 422)
        if target.scheme != "https":
            fail("REAP_HTTPS_REQUIRED", "Reap card setup requires an HTTPS payment return URL. Update PAYMENT_RETURN_URL and restart the backend.", 422)
        with db.transaction() as s:
            user = row(s, User, user_id)
            existing_id = user.enrollment_id
        try:
            result = reap.get_enrollment(existing_id) if existing_id else reap.create_enrollment(
                user_id, body.email, body.return_url, "enrollment-" + fingerprint({"user": user_id, "key": idempotency_key}))
        except ProviderError as error:
            provider_failure(error)
        if not result.get("id"):
            fail("PROVIDER_RESPONSE_INVALID", "Enrollment response lacks an ID", 502)
        with db.transaction(write=True) as s:
            user = row(s, User, user_id, lock=True)
            if user.enrollment_id and user.enrollment_id != result["id"]:
                fail("ENROLLMENT_CONFLICT", "Another enrollment is already linked")
            user.enrollment_id = result["id"]
        return {"enrollment_id": result["id"], "status": result.get("status"), "nextAction": result.get("nextAction")}

    @api.put("/v1/users/{user_id}/enrollment", tags=["Reap Sandbox"])
    def attach_enrollment(user_id: str, body: AttachEnrollmentIn, _=Depends(service)):
        require_reap()
        try:
            result = reap.get_enrollment(body.enrollment_id)
        except ProviderError as error:
            provider_failure(error)
        owner = result.get("owner", {})
        if owner.get("type") != "CLIENT_REFERENCE" or owner.get("id") != user_id:
            fail("ENROLLMENT_OWNER_MISMATCH", "Enrollment must belong to this BookPool user", 422)
        if result.get("status") != "ACTIVE":
            fail("ENROLLMENT_NOT_ACTIVE", "Complete hosted card entry first")
        with db.transaction(write=True) as s:
            row(s, User, user_id, lock=True).enrollment_id = body.enrollment_id
        return {"enrollment_id": body.enrollment_id, "status": "ACTIVE"}

    @api.post("/v1/groups/{group_id}/reap-quote", tags=["Reap Sandbox"])
    def quote_group(group_id: str, body: QuoteIn, _=Depends(service)):
        require_reap()
        with db.transaction() as s:
            group = row(s, Group, group_id)
            version(group, body.version)
            if group.state not in {"PROPOSED", "READY"}:
                fail("INVALID_STATE", "Cannot quote after checkout begins")
            if not group.shipping_address:
                fail("ADDRESS_REQUIRED", "Reap quote needs the shared Singapore shipping address", 422)
            ps = participants(s, group.id)
            if any(row(s, Offer, p.offer_id).source != "reap" for p in ps):
                fail("REAP_VARIANTS_REQUIRED", "Use offers with source=reap and variant IDs resolved through Reap discovery")
            quantities = {}
            for p in ps:
                variant_id = row(s, Offer, p.offer_id).variant_id
                quantities[variant_id] = quantities.get(variant_id, 0) + row(s, BookRequest, p.request_id).quantity
            items = [{"variantId": variant_id, "quantity": quantity}
                     for variant_id, quantity in quantities.items()]
            address = group.shipping_address
        try:
            result = reap.create_quote(items, body.email, address, body.offer_code, uid())
            if body.shipping_option_id:
                result = reap.select_shipping(result["id"], body.shipping_option_id, uid())
            total = money_minor(result["amountBreakdown"]["finalAmount"])
            expiry = timestamp(result["expiresAt"])
            quote_id = result["id"]
        except ProviderError as error:
            provider_failure(error)
        except (KeyError, TypeError, ValueError):
            fail("PROVIDER_RESPONSE_INVALID", "Quote response is incomplete", 502)
        if not future(expiry):
            fail("QUOTE_EXPIRED", "Provider returned an expired quote", 502)
        with db.transaction(write=True) as s:
            group = row(s, Group, group_id, lock=True)
            version(group, body.version)
            if group.state not in {"PROPOSED", "READY"}:
                fail("INVALID_STATE", "Group changed during quote creation")
            ps = participants(s, group.id)
            shares = allocate(total, [p.amount_minor for p in ps])
            expiries = [expiry]
            for p, share in zip(ps, shares):
                req = row(s, BookRequest, p.request_id)
                offer = row(s, Offer, p.offer_id)
                if share <= 0 or share > req.budget_minor or individual_baseline(s, req) - share < req.min_savings_minor:
                    fail("QUOTE_CONSTRAINT_VIOLATION", "Live quote violates participant budgets or minimum savings")
                expiries.append(timestamp(offer.expires_at))
                p.amount_minor = share
            group.total_minor = total
            group.expires_at = min(expiries).isoformat()
            group.quote_id = quote_id
            invalidate(s, group, "REAP_QUOTE_READY")
            # Clear consent even when price did not change: new quote = new version.
            return {"group": group_view(s, group), "shipping_options": result.get("shippingOptions", []),
                    "amount_breakdown": result["amountBreakdown"]}

    def apply_result(order_id, result):
        with db.transaction(write=True) as s:
            order = row(s, Order, order_id, lock=True)
            group = row(s, Group, order.group_id, lock=True)
            if order.state in {"ORDERED", "FAILED", "REFUNDED", "REFUND_REQUESTED", "RECONCILIATION_REQUIRED"}:
                return order_view(order)
            status = result.get("status", "UNKNOWN")
            if result.get("id"):
                order.provider_checkout_id = result["id"]
            if status == "COMPLETED":
                try:
                    amount = money_minor(result.get("finalAmount"))
                except ProviderError:
                    order.state = group.state = "RECONCILIATION_REQUIRED"
                    order.error_code = "FINAL_AMOUNT_MISSING"
                    emit(s, "RECONCILIATION_REQUIRED", group.id, order_id=order.id)
                    return order_view(order)
                order.final_minor = amount
                order.merchant_order_id = result.get("orderId")
                if amount > group.total_minor or not order.merchant_order_id:
                    order.state = group.state = "RECONCILIATION_REQUIRED"
                    order.error_code = "FINAL_AMOUNT_OR_REFERENCE_MISMATCH"
                    emit(s, "RECONCILIATION_REQUIRED", group.id, order_id=order.id)
                else:
                    order.state = group.state = "ORDERED"
                    order.approval_url = None
                    for p in participants(s, group.id):
                        if order.provider == "mock":
                            journal(s, group, p, "CAPTURE")
                        p.payment_state = "CAPTURED" if order.provider == "mock" else "NOT_COLLECTED"
                        row(s, BookRequest, p.request_id, lock=True).status = "ORDERED"
                    emit(s, "ORDER_PLACED", group.id, order_id=order.id, simulated=order.provider == "mock",
                         merchant_order_id=order.merchant_order_id, final_minor=amount)
            elif status in {"FAILED", "EXPIRED", "CANCELLED"}:
                order.state = group.state = "FAILED"
                order.error_code = "PROVIDER_" + status
                void_holds(s, group)
                release_requests(s, group)
                emit(s, "ORDER_FAILED", group.id, order_id=order.id, code=order.error_code)
            elif status == "REQUIRES_ACTION":
                order.state = group.state = "AWAITING_PAYMENT_APPROVAL"
                order.approval_url = (result.get("nextAction") or {}).get("url")
                emit(s, "PAYMENT_APPROVAL_REQUIRED", group.id, order_id=order.id)
            else:
                order.state = group.state = "PAYMENT_PENDING"
            return order_view(order)

    def uncertain(order_id, code):
        with db.transaction(write=True) as s:
            order = row(s, Order, order_id, lock=True)
            if order.state not in {"CHECKOUT_PENDING", "PAYMENT_UNKNOWN", "PAYMENT_PENDING", "AWAITING_PAYMENT_APPROVAL"}:
                return order_view(order)
            group = row(s, Group, order.group_id, lock=True)
            order.state = group.state = "PAYMENT_UNKNOWN"
            order.error_code = code
            emit(s, "PAYMENT_OUTCOME_UNKNOWN", group.id, order_id=order.id, code=code)
            return order_view(order)

    @api.post("/v1/groups/{group_id}/execute", tags=["Orders"])
    def execute(group_id: str, body: ExecuteIn, _=Depends(service)):
        # If an intent exists, never start another checkout. Reconciliation uses that intent.
        with db.transaction() as s:
            group = row(s, Group, group_id)
            version(group, body.version)
            existing = s.scalar(select(Order).where(Order.group_id == group.id))
            if existing:
                return order_view(existing)
            quote_id = group.quote_id
            purchaser = row(s, User, group.purchaser_id)
            enrollment_id = purchaser.enrollment_id
            purchaser_id = purchaser.id
        if settings.payment_mode == "reap_sandbox":
            if body.outcome != "success":
                fail("MOCK_CONTROL_DISABLED", "Failure injection is available only in mock mode", 422)
            if not quote_id or not enrollment_id:
                fail("REAP_SETUP_REQUIRED", "Create a Reap quote and enroll the designated purchaser first")
            try:
                enrollment_result = reap.get_enrollment(enrollment_id)
                owner = enrollment_result.get("owner", {})
                if enrollment_result.get("status") != "ACTIVE" or owner.get("id") != purchaser_id or owner.get("type") != "CLIENT_REFERENCE":
                    fail("ENROLLMENT_NOT_ACTIVE", "Purchaser enrollment must be active and owned by the purchaser")
                live = reap.get_quote(quote_id)
                if not future(live["expiresAt"]):
                    fail("QUOTE_EXPIRED", "Requote and collect fresh approvals")
                live_total = money_minor(live["amountBreakdown"]["finalAmount"])
            except ProviderError as error:
                provider_failure(error)
            except (KeyError, TypeError, ValueError):
                fail("PROVIDER_RESPONSE_INVALID", "Cannot verify provider quote", 502)
        with db.transaction(write=True) as s:
            group = row(s, Group, group_id, lock=True)
            existing = s.scalar(select(Order).where(Order.group_id == group.id))
            if existing:
                return order_view(existing)
            ensure_ready(s, group, body.version, settings.payment_mode)
            if settings.payment_mode == "reap_sandbox" and live_total != group.total_minor:
                fail("PRICE_CHANGED", "Requote and collect fresh approval before checkout")
            order = Order(group_id=group.id, provider=settings.payment_mode, total_minor=group.total_minor)
            s.add(order)
            s.flush()
            order.checkout_payload = {
                "quoteId": group.quote_id, "enrollmentId": enrollment_id,
                "presentation": {"type": "REDIRECT", "returnUrl": settings.hosted_return_url}}
            group.state = "CHECKOUT_PENDING"
            emit(s, "CHECKOUT_STARTED", group.id, order_id=order.id, provider=order.provider)
            order_id, payload, total = order.id, order.checkout_payload, order.total_minor
        if settings.payment_mode == "mock":
            if body.outcome == "timeout":
                return uncertain(order_id, "MOCK_TIMEOUT")
            return apply_result(order_id, {
                "id": "mock-" + order_id, "status": "FAILED" if body.outcome == "decline" else "COMPLETED",
                "orderId": "SIM-" + order_id[:8], "finalAmount": {"amount": f"{total // 100}.{total % 100:02d}", "currency": "SGD"}})
        try:
            return apply_result(order_id, reap.create_checkout(payload, "checkout-" + order_id))
        except ProviderError as error:
            if error.uncertain:
                return uncertain(order_id, error.code)
            return apply_result(order_id, {"status": "FAILED"})

    @api.get("/v1/orders/{order_id}", tags=["Orders"])
    def get_order(order_id: str, actor=Depends(auth)):
        with db.transaction() as s:
            order = row(s, Order, order_id)
            can_view(s, actor, row(s, Group, order.group_id))
            return order_view(order)

    @api.post("/v1/orders/{order_id}/reconcile", tags=["Orders"])
    def reconcile(order_id: str, _=Depends(service)):
        with db.transaction() as s:
            order = row(s, Order, order_id)
            if order.state in {"ORDERED", "FAILED", "REFUNDED", "REFUND_REQUESTED", "RECONCILIATION_REQUIRED"}:
                return order_view(order)
            if order.provider == "mock":
                result = {"id": "mock-" + order.id, "status": "COMPLETED", "orderId": "SIM-" + order.id[:8],
                          "finalAmount": {"amount": str(order.total_minor / 100), "currency": "SGD"}}
                return apply_result(order_id, result)
            checkout_id, payload, created_at = order.provider_checkout_id, order.checkout_payload, order.created_at
        try:
            if checkout_id:
                result = reap.get_checkout(checkout_id)
            else:
                # Provider idempotency retention is 24h. Do not replay old ambiguous intents.
                if timestamp(created_at) < datetime.now(timezone.utc) - timedelta(hours=23):
                    return uncertain(order_id, "MANUAL_RECONCILIATION_REQUIRED")
                result = reap.create_checkout(payload, "checkout-" + order_id)
            return apply_result(order_id, result)
        except ProviderError as error:
            # A previously ambiguous charge stays ambiguous even if a retry gets a validation error.
            return uncertain(order_id, error.code)

    @api.post("/v1/orders/{order_id}/refund", tags=["Orders"])
    def refund(order_id: str, body: ReasonIn, _=Depends(service)):
        with db.transaction(write=True) as s:
            order = row(s, Order, order_id, lock=True)
            group = row(s, Group, order.group_id, lock=True)
            if order.state in {"REFUNDED", "REFUND_REQUESTED"}:
                return order_view(order)
            if order.state != "ORDERED":
                fail("INVALID_STATE", "Only a confirmed order can enter the refund workflow")
            if order.provider == "mock":
                order.state = group.state = "REFUNDED"
                for p in participants(s, group.id):
                    journal(s, group, p, "REFUND")
                    p.payment_state = "REFUNDED"
                    req = row(s, BookRequest, p.request_id, lock=True)
                    req.status = "OPEN"
                release_requests(s, group)
                emit(s, "MOCK_REFUND_COMPLETED", group.id, order_id=order.id,
                     amount_minor=order.final_minor, reason=body.reason, simulated=True)
            else:
                # No documented merchant-refund endpoint in Agentic; do not invent one.
                order.state = group.state = "REFUND_REQUESTED"
                emit(s, "REFUND_MANUAL_REVIEW_REQUIRED", group.id, order_id=order.id, reason=body.reason)
            return order_view(order)

    @api.get("/v1/payments", tags=["Commitments"])
    def payment_ledger(group_id: str | None = None, limit: int = Query(100, ge=1, le=500), actor=Depends(auth)):
        with db.transaction() as s:
            stmt = select(PaymentTransaction).order_by(PaymentTransaction.created_at).limit(limit)
            if not actor["service"]:
                stmt = stmt.where(PaymentTransaction.user_id == actor["user_id"])
            if group_id:
                stmt = stmt.where(PaymentTransaction.group_id == group_id)
            return [model_dict(p) for p in s.scalars(stmt)]

    @api.get("/v1/events", tags=["Integration Events"])
    def events(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), _=Depends(service)):
        with db.transaction() as s:
            records = list(s.scalars(select(Event).where(Event.id > after).order_by(Event.id).limit(limit)))
            return {"events": [model_dict(e) for e in records], "next_cursor": records[-1].id if records else after}

    @api.get("/payment-return", response_class=HTMLResponse, include_in_schema=False)
    def payment_return():
        return "<h1>Return to BookPool</h1><p>Your approval was submitted. BookPool will confirm the payment with Reap before reporting an order.</p>"

    return api


def app_factory():
    return create_app()
