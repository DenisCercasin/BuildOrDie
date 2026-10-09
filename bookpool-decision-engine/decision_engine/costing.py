"""Cost calculations: what a request costs alone, and what a group order costs.

Everything here is pure and deterministic; no LLM involvement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .models import BookRequest, EngineConfig, MerchantOffer, MerchantPolicy, VolumeTier
from .money import ZERO, q, split_amount, total


# --------------------------------------------------------------------------- context


class OfferBook:
    """Index of usable offers by (merchant_id, book_id), restricted to approved merchants."""

    def __init__(self, offers: list[MerchantOffer], policies: list[MerchantPolicy]):
        self.policies: dict[str, MerchantPolicy] = {
            p.merchant_id: p for p in policies if p.approved
        }
        best: dict[tuple[str, str], MerchantOffer] = {}
        for o in offers:
            if o.merchant_id not in self.policies or not o.in_stock:
                continue
            key = (o.merchant_id, o.book_id)
            cur = best.get(key)
            # keep the cheapest, then fastest, duplicate offer
            if cur is None or (o.price, o.delivery_days) < (cur.price, cur.delivery_days):
                best[key] = o
        self._offers = best

    def get(self, merchant_id: str, book_id: str) -> MerchantOffer | None:
        return self._offers.get((merchant_id, book_id))

    def merchant_ids(self) -> list[str]:
        return sorted(self.policies)

    def allowed_merchants(self, req: BookRequest) -> list[str]:
        """Approved merchants that sell the book, serve the pickup point and the user accepts."""
        out = []
        for mid in self.merchant_ids():
            pol = self.policies[mid]
            if req.preferred_merchants is not None and mid not in req.preferred_merchants:
                continue
            if pol.delivers_to is not None and req.pickup_point not in pol.delivers_to:
                continue
            offer = self.get(mid, req.book_id)
            if offer is None or not stock_ok(offer, req.quantity):
                continue
            out.append(mid)
        return out

    @property
    def uses_simulated_data(self) -> bool:
        return any(o.is_simulated for o in self._offers.values()) or any(
            p.is_simulated for p in self.policies.values()
        )


@dataclass(frozen=True)
class Ctx:
    today: date
    cfg: EngineConfig
    book: OfferBook


def stock_ok(offer: MerchantOffer, qty: int) -> bool:
    return offer.in_stock and (offer.stock_qty is None or offer.stock_qty >= qty)


def expected_delivery(ctx: Ctx, delivery_days: int) -> date:
    return ctx.today + timedelta(days=ctx.cfg.order_processing_days + delivery_days)


def latest_order_date(ctx: Ctx, deadline: date, delivery_days: int) -> date:
    """Last day the order can be placed (after processing time) and still arrive by deadline."""
    return deadline - timedelta(days=ctx.cfg.order_processing_days + delivery_days)


def discount_tier(policy: MerchantPolicy, items: int) -> VolumeTier | None:
    tiers = [t for t in policy.volume_discounts if items >= t.min_items]
    return max(tiers, key=lambda t: t.min_items) if tiers else None


def next_discount_tier(policy: MerchantPolicy, items: int) -> VolumeTier | None:
    tiers = [t for t in policy.volume_discounts if t.min_items > items]
    return min(tiers, key=lambda t: t.min_items) if tiers else None


def shipping_for(policy: MerchantPolicy, order_value: Decimal) -> Decimal:
    if policy.free_shipping_threshold is not None and order_value >= policy.free_shipping_threshold:
        return ZERO
    return q(policy.shipping_fee)


# --------------------------------------------------------------------------- individual


@dataclass(frozen=True)
class IndividualOption:
    merchant_id: str
    unit_price: Decimal
    item_subtotal: Decimal
    discount: Decimal
    shipping: Decimal
    total: Decimal
    delivery_days: int
    expected_delivery: date
    meets_deadline: bool
    within_budget: bool


def individual_options(req: BookRequest, ctx: Ctx) -> list[IndividualOption]:
    """Every way this request could be bought on its own, cheapest first."""
    opts: list[IndividualOption] = []
    for mid in ctx.book.allowed_merchants(req):
        pol = ctx.book.policies[mid]
        offer = ctx.book.get(mid, req.book_id)
        assert offer is not None
        subtotal = q(offer.price * req.quantity)
        tier = discount_tier(pol, req.quantity)
        discount = q(subtotal * tier.percent_off / 100) if tier else ZERO
        value = subtotal - discount
        ship = shipping_for(pol, value)
        tot = q(value + ship)
        eta = expected_delivery(ctx, offer.delivery_days)
        opts.append(
            IndividualOption(
                merchant_id=mid,
                unit_price=q(offer.price),
                item_subtotal=subtotal,
                discount=discount,
                shipping=ship,
                total=tot,
                delivery_days=offer.delivery_days,
                expected_delivery=eta,
                meets_deadline=eta <= req.deadline,
                within_budget=req.max_budget is None or tot <= req.max_budget,
            )
        )
    opts.sort(key=lambda o: (o.total, o.expected_delivery, o.merchant_id))
    return opts


def baseline(req: BookRequest, ctx: Ctx) -> IndividualOption | None:
    """The 'buy it alone' benchmark used to measure savings.

    Prefers the cheapest option that meets the deadline; if none does, falls back
    to the cheapest option at all (flagged by ``meets_deadline=False``).
    """
    opts = individual_options(req, ctx)
    on_time = [o for o in opts if o.meets_deadline]
    if on_time:
        return on_time[0]
    return opts[0] if opts else None


# --------------------------------------------------------------------------- group


@dataclass(frozen=True)
class LineCost:
    request_id: str
    unit_price: Decimal
    quantity: int
    item_subtotal: Decimal
    discount: Decimal
    shipping_share: Decimal
    total: Decimal


@dataclass(frozen=True)
class GroupCosting:
    merchant_id: str
    lines: tuple[LineCost, ...]
    item_count: int
    subtotal: Decimal
    discount_pct: Decimal
    discount_total: Decimal
    shipping_fee: Decimal
    total: Decimal
    free_shipping_unlocked: bool
    amount_to_free_shipping: Decimal | None
    delivery_days: int
    expected_delivery: date
    min_stock_headroom: int | None  # smallest (stock_qty - qty needed) across books, None if unknown


def cost_group(reqs: list[BookRequest], merchant_id: str, ctx: Ctx) -> GroupCosting | None:
    """Cost of buying all ``reqs`` in one order at ``merchant_id``; None if impossible."""
    pol = ctx.book.policies.get(merchant_id)
    if pol is None:
        return None

    needed: dict[str, int] = {}
    for r in reqs:
        needed[r.book_id] = needed.get(r.book_id, 0) + r.quantity

    headroom: int | None = None
    max_days = 0
    for book_id, qty in needed.items():
        offer = ctx.book.get(merchant_id, book_id)
        if offer is None or not stock_ok(offer, qty):
            return None
        if offer.stock_qty is not None:
            h = offer.stock_qty - qty
            headroom = h if headroom is None else min(headroom, h)
        max_days = max(max_days, offer.delivery_days)

    subtotals: list[Decimal] = []
    units: list[Decimal] = []
    for r in reqs:
        offer = ctx.book.get(merchant_id, r.book_id)
        assert offer is not None
        units.append(q(offer.price))
        subtotals.append(q(offer.price * r.quantity))

    subtotal = total(subtotals)
    items = sum(r.quantity for r in reqs)
    tier = discount_tier(pol, items)
    pct = tier.percent_off if tier else Decimal(0)
    discount_total = q(subtotal * pct / 100)
    discounts = split_amount(discount_total, subtotals)
    value = subtotal - discount_total
    ship = shipping_for(pol, value)

    weights = [Decimal(1)] * len(reqs) if ctx.cfg.cost_split == "equal" else subtotals
    shares = split_amount(ship, weights)

    lines = tuple(
        LineCost(
            request_id=r.request_id,
            unit_price=units[i],
            quantity=r.quantity,
            item_subtotal=subtotals[i],
            discount=discounts[i],
            shipping_share=shares[i],
            total=q(subtotals[i] - discounts[i] + shares[i]),
        )
        for i, r in enumerate(reqs)
    )

    threshold = pol.free_shipping_threshold
    to_free = None if threshold is None else q(max(ZERO, threshold - value))
    return GroupCosting(
        merchant_id=merchant_id,
        lines=lines,
        item_count=items,
        subtotal=subtotal,
        discount_pct=pct,
        discount_total=discount_total,
        shipping_fee=ship,
        total=q(value + ship),
        free_shipping_unlocked=ship == ZERO and q(pol.shipping_fee) > ZERO,
        amount_to_free_shipping=to_free,
        delivery_days=max_days,
        expected_delivery=expected_delivery(ctx, max_days),
        min_stock_headroom=headroom,
    )
