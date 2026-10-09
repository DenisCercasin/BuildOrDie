import hashlib
import json
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select

from .db import BookRequest, Event, Group, Offer, Order, Participant, PaymentTransaction, User


def fail(code, message, status=409):
    raise HTTPException(status, {"code": code, "message": message})


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        fail("TIMEZONE_REQUIRED", "Timestamp must include a timezone", 422)
    return parsed.astimezone(timezone.utc)


def future(value):
    return timestamp(value) > datetime.now(timezone.utc)


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def row(session, model, ident, lock=False):
    stmt = select(model).where(model.id == ident)
    if lock:
        stmt = stmt.with_for_update()
    found = session.scalar(stmt)
    if not found:
        fail("NOT_FOUND", "Record not found", 404)
    return found


def participants(session, gid):
    return list(session.scalars(select(Participant).where(Participant.group_id == gid)
                               .order_by(Participant.id)))


def emit(session, kind, gid=None, **payload):
    session.add(Event(group_id=gid, kind=kind, payload=payload))


def version(group, requested):
    if group.version != requested:
        fail("STALE_PROPOSAL", "Reload the current proposal and approve its new version")


def open_group(group):
    if group.state not in {"PROPOSED", "READY"}:
        fail("INVALID_STATE", f"Group is {group.state}")
    if not future(group.expires_at):
        fail("PROPOSAL_EXPIRED", "Create a fresh proposal before requesting approval")


def invalidate(session, group, kind):
    void_holds(session, group)
    group.version += 1
    group.state = "PROPOSED"
    for p in participants(session, group.id):
        p.approved_version = None
        p.payment_state = "PENDING"
    emit(session, kind, group.id, version=group.version)


def journal(session, group, participant, kind):
    existing = session.scalar(select(PaymentTransaction).where(
        PaymentTransaction.group_id == group.id, PaymentTransaction.version == group.version,
        PaymentTransaction.request_id == participant.request_id, PaymentTransaction.kind == kind))
    if not existing:
        session.add(PaymentTransaction(group_id=group.id, user_id=participant.user_id,
                                      request_id=participant.request_id, version=group.version,
                                      kind=kind, amount_minor=participant.amount_minor))


def void_holds(session, group):
    for p in participants(session, group.id):
        if p.payment_state == "AUTHORIZED":
            journal(session, group, p, "VOID")
            p.payment_state = "VOIDED"


def individual_baseline(session, req):
    candidates = session.scalars(select(Offer).where(Offer.request_id == req.id, Offer.available.is_(True)))
    feasible = [o.individual_total_minor for o in candidates
                if future(o.expires_at) and timestamp(o.delivery_at) <= timestamp(req.latest_delivery_at)]
    if not feasible:
        fail("NO_INDIVIDUAL_BASELINE", "No fresh, feasible individual offer is available")
    return min(feasible)


def refresh_ready(session, group, mode):
    ps = participants(session, group.id)
    ready = all(p.approved_version == group.version and
                p.payment_state == "AUTHORIZED" for p in ps)
    group.state = "READY" if ready else "PROPOSED"


def validate_proposal(session, payload, merchants, existing_id=None):
    if payload.merchant not in merchants:
        fail("MERCHANT_NOT_ALLOWED", "Choose an approved Singapore merchant", 422)
    if sum(a.amount_minor for a in payload.allocations) != payload.total_minor:
        fail("ALLOCATION_MISMATCH", "Participant allocations must equal the complete cart total", 422)
    ids = [a.request_id for a in payload.allocations]
    if len(ids) != len(set(ids)):
        fail("DUPLICATE_REQUEST", "A request may appear only once", 422)
    if not future(payload.expires_at) or not future(payload.delivery_at):
        fail("EXPIRED", "Proposal and delivery timestamps must be in the future", 422)
    result = []
    # Stable lock order prevents deadlocks when optimizers reserve overlapping requests.
    requests = {rid: row(session, BookRequest, rid, lock=True) for rid in sorted(ids)}
    for a in payload.allocations:
        req = requests[a.request_id]
        offer = row(session, Offer, a.offer_id)
        if req.status != "OPEN" or req.group_id not in {None, existing_id}:
            fail("REQUEST_RESERVED", "Request is already in another active order")
        if offer.request_id != req.id or offer.merchant != payload.merchant:
            fail("OFFER_MISMATCH", "Offer must match the request and selected merchant", 422)
        if not offer.available or not future(offer.expires_at):
            fail("OFFER_UNAVAILABLE", "Offer is unavailable or expired")
        if timestamp(payload.expires_at) > timestamp(offer.expires_at):
            fail("EXPIRY_MISMATCH", "Proposal must expire no later than its earliest offer", 422)
        if a.amount_minor > req.budget_minor:
            fail("BUDGET_EXCEEDED", "Allocated total exceeds a participant's budget", 422)
        if individual_baseline(session, req) - a.amount_minor < req.min_savings_minor:
            fail("MINIMUM_SAVINGS", "A participant's minimum savings is not met", 422)
        if payload.pickup_location != req.pickup_location:
            fail("PICKUP_MISMATCH", "Participants must agree to the same collection point", 422)
        if timestamp(payload.delivery_at) > timestamp(req.latest_delivery_at):
            fail("DELIVERY_DEADLINE", "Expected delivery misses a participant's deadline", 422)
        if timestamp(payload.delivery_at) < timestamp(offer.delivery_at):
            fail("DELIVERY_ESTIMATE", "Proposal cannot promise delivery earlier than the merchant offer", 422)
        result.append((req, offer, a))
    if payload.purchaser_id not in {r.user_id for r, _, _ in result}:
        fail("PURCHASER_NOT_MEMBER", "Designated purchaser must be a participant", 422)
    return result


def model_dict(obj):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def group_view(session, group):
    data = model_dict(group)
    data.pop("fingerprint")
    data.pop("shipping_address")
    data["participants"] = []
    for p in participants(session, group.id):
        entry = model_dict(p)
        offer = row(session, Offer, p.offer_id)
        req = row(session, BookRequest, p.request_id)
        # Preserve viewability of historical orders after source offers expire.
        candidates = list(session.scalars(select(Offer).where(Offer.request_id == req.id)))
        feasible = [o.individual_total_minor for o in candidates if o.available and future(o.expires_at)
                    and timestamp(o.delivery_at) <= timestamp(req.latest_delivery_at)]
        baseline = min(feasible) if feasible else offer.individual_total_minor
        entry.update(title=req.title, individual_total_minor=baseline,
                     savings_minor=baseline - p.amount_minor,
                     payment_is_simulated=p.payment_state != "NOT_COLLECTED")
        data["participants"].append(entry)
    order = session.scalar(select(Order).where(Order.group_id == group.id))
    if order:
        data["order"] = order_view(order)
    return data


def order_view(order):
    data = model_dict(order)
    data.pop("checkout_payload")
    data["simulated"] = order.provider == "mock"
    return data


def release_requests(session, group):
    for p in participants(session, group.id):
        req = row(session, BookRequest, p.request_id, lock=True)
        req.group_id = None


def ensure_ready(session, group, requested, mode):
    version(group, requested)
    open_group(group)
    refresh_ready(session, group, mode)
    if group.state != "READY":
        fail("COMMITMENTS_MISSING", "Every participant must approve and confirm their simulated contribution")
    for p in participants(session, group.id):
        req = row(session, BookRequest, p.request_id)
        offer = row(session, Offer, p.offer_id)
        if not future(offer.expires_at) or not offer.available:
            fail("OFFER_UNAVAILABLE", "Refresh offers and re-propose before checkout")
        if p.amount_minor > req.budget_minor or not future(req.latest_delivery_at):
            fail("CONSTRAINT_CHANGED", "Participant budget or deadline no longer permits this order")
        if individual_baseline(session, req) - p.amount_minor < req.min_savings_minor:
            fail("MINIMUM_SAVINGS", "A better individual offer means the proposal no longer meets minimum savings")
