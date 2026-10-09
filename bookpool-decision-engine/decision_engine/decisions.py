"""Agentic decisions: buy now or wait?

The rule of thumb: waiting is only worth it if the *expected* extra saving per
person from more buyers joining exceeds ``buy_now_marginal_threshold``. Some
situations override that and trigger an immediate buy-now recommendation:
the deadline is close, stock is running low, or the price just dropped.

Arrivals of compatible buyers are modelled as a Poisson process with rate
``expected_arrivals_per_day``. That's crude but transparent, and the rate can be
re-estimated from real request history later.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

from .costing import Ctx, IndividualOption, latest_order_date, next_discount_tier
from .models import Action, BookRequest, MerchantOffer, ReasonCode, Recommendation
from .money import ZERO, fmt, q
from .optimizer import BlockPlan


def prob_at_least(n_needed: int, rate_per_day: float, days: int) -> float:
    """P(N >= n_needed) for N ~ Poisson(rate * days)."""
    if n_needed <= 0:
        return 1.0
    mu = rate_per_day * max(days, 0)
    if mu <= 0:
        return 0.0
    cdf = sum(math.exp(-mu) * mu**k / math.factorial(k) for k in range(n_needed))
    return max(0.0, min(1.0, 1.0 - cdf))


def price_drops(
    plan: BlockPlan, ctx: Ctx, previous_offers: list[MerchantOffer] | None
) -> list[str]:
    """Book ids in this group whose price fell by at least price_drop_percent."""
    if not previous_offers:
        return []
    prev = {(o.merchant_id, o.book_id): o.price for o in previous_offers}
    out = []
    for r in plan.requests:
        old = prev.get((plan.costing.merchant_id, r.book_id))
        cur = ctx.book.get(plan.costing.merchant_id, r.book_id)
        if old and cur and old > 0:
            drop_pct = (old - cur.price) / old * 100
            if drop_pct >= ctx.cfg.price_drop_percent and r.book_id not in out:
                out.append(r.book_id)
    return out


def recommend_for_group(
    plan: BlockPlan, ctx: Ctx, previous_offers: list[MerchantOffer] | None = None
) -> Recommendation:
    cfg = ctx.cfg
    gc = plan.costing
    pol = ctx.book.policies[gc.merchant_id]
    k = len(plan.requests)

    order_by = min(latest_order_date(ctx, r.deadline, gc.delivery_days) for r in plan.requests)
    days_left = (order_by - ctx.today).days

    # --- what could more buyers still unlock?
    gain_per_person = ZERO
    needed_buyers = 0
    what = ""
    if not gc.free_shipping_unlocked and gc.shipping_fee > 0 and gc.amount_to_free_shipping:
        needed_buyers = max(1, math.ceil(gc.amount_to_free_shipping / cfg.typical_book_price))
        # today everyone pays fee/k; if unlocked they pay nothing
        gain_per_person = q(gc.shipping_fee / k)
        what = f"free delivery (needs about {fmt(gc.amount_to_free_shipping)} more in books)"
    else:
        nxt = next_discount_tier(pol, gc.item_count)
        if nxt is not None:
            needed_buyers = nxt.min_items - gc.item_count
            cur_pct = gc.discount_pct
            extra_pct = nxt.percent_off - cur_pct
            gain_per_person = q(gc.subtotal * extra_pct / 100 / k)
            what = f"a {nxt.percent_off}% volume discount at {nxt.min_items} books"

    p_more = prob_at_least(needed_buyers, cfg.expected_arrivals_per_day, days_left) if needed_buyers else 0.0
    expected_extra = q(gain_per_person * Decimal(str(round(p_more, 4))))

    # --- decide
    reasons: list[ReasonCode] = []
    if gc.free_shipping_unlocked:
        reasons.append(ReasonCode.FREE_SHIPPING_UNLOCKED)
    if days_left <= cfg.deadline_warning_days:
        reasons.append(ReasonCode.DEADLINE_APPROACHING)
    if gc.min_stock_headroom is not None and gc.min_stock_headroom <= cfg.low_stock_threshold:
        reasons.append(ReasonCode.LOW_STOCK)
    drops = price_drops(plan, ctx, previous_offers)
    if drops:
        reasons.append(ReasonCode.PRICE_DROP)
    if k >= cfg.max_group_size:
        reasons.append(ReasonCode.MAX_GROUP_SIZE)

    urgent = {
        ReasonCode.DEADLINE_APPROACHING,
        ReasonCode.LOW_STOCK,
        ReasonCode.PRICE_DROP,
        ReasonCode.MAX_GROUP_SIZE,
    }
    low_marginal = expected_extra < cfg.buy_now_marginal_threshold
    if low_marginal:
        reasons.append(ReasonCode.LOW_MARGINAL_VALUE)

    if low_marginal or urgent & set(reasons):
        action = Action.BUY_NOW
    else:
        action = Action.WAIT
        reasons.append(ReasonCode.WAITING_LIKELY_PAYS_OFF)

    # --- explanation (deterministic; the bot can rephrase it)
    parts: list[str] = []
    if action is Action.BUY_NOW:
        if ReasonCode.DEADLINE_APPROACHING in reasons:
            parts.append(f"The order must be placed by {order_by:%d %b} to meet everyone's deadline.")
        if ReasonCode.LOW_STOCK in reasons:
            parts.append("Stock is running low at this merchant.")
        if ReasonCode.PRICE_DROP in reasons:
            parts.append("A price in this order just dropped.")
        if ReasonCode.MAX_GROUP_SIZE in reasons:
            parts.append("The group is full.")
        if low_marginal:
            if what:
                parts.append(
                    f"Waiting could unlock {what}, but that's worth only about "
                    f"{fmt(expected_extra)} per person in expectation."
                )
            else:
                parts.append("More buyers wouldn't reduce anyone's cost further.")
    else:
        parts.append(
            f"There's a {p_more:.0%} chance enough buyers join by {order_by:%d %b} to unlock {what}, "
            f"worth about {fmt(gain_per_person)} per person."
        )

    return Recommendation(
        action=action,
        reasons=reasons,
        expected_extra_saving_per_person=expected_extra,
        probability_more_buyers=round(p_more, 4),
        order_by=order_by,
        days_left_to_order=days_left,
        explanation=" ".join(parts),
    )


def solo_outlook(
    req: BookRequest, best: IndividualOption | None, ctx: Ctx
) -> tuple[str, Decimal, date | None, list[str]]:
    """Status for an ungrouped request: (status, potential_saving, order_by, reasons)."""
    from .models import SoloStatus

    if best is None:
        return SoloStatus.UNSERVABLE.value, ZERO, None, [
            "No approved merchant currently has this book in stock for your pickup point."
        ]
    if not best.meets_deadline:
        return SoloStatus.DEADLINE_UNREACHABLE.value, ZERO, None, [
            f"Even ordered today, the fastest option arrives {best.expected_delivery:%d %b}, "
            f"after your deadline of {req.deadline:%d %b}."
        ]

    order_by = latest_order_date(ctx, req.deadline, best.delivery_days)
    days_left = (order_by - ctx.today).days
    potential = best.shipping  # the most a group can save you is your delivery fee (plus discounts)

    if potential <= 0:
        return SoloStatus.BUY_SOLO_RECOMMENDED.value, ZERO, order_by, [
            f"Delivery is already free on your own at {best.merchant_id}, so a group can't save you anything."
        ]
    if potential < req.min_savings_to_wait:
        return SoloStatus.BUY_SOLO_RECOMMENDED.value, potential, order_by, [
            f"The most a group could save you is about {fmt(potential)}, below the "
            f"{fmt(req.min_savings_to_wait)} you said makes waiting worthwhile."
        ]
    if days_left <= ctx.cfg.deadline_warning_days:
        return SoloStatus.BUY_SOLO_RECOMMENDED.value, potential, order_by, [
            f"No group has formed yet and you'd need to order by {order_by:%d %b} to make your deadline."
        ]
    p = prob_at_least(1, ctx.cfg.expected_arrivals_per_day, days_left)
    return SoloStatus.WAITING_FOR_GROUP.value, potential, order_by, [
        f"Waiting for a compatible buyer: grouping could save you up to {fmt(potential)}. "
        f"About {p:.0%} chance someone joins by {order_by:%d %b}; we'll tell you if not."
    ]
