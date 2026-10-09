"""DecisionEngine — the single entry point other components call.

Stateless: pass in the current open requests and offers (plus, optionally, the
previous result) and get back group proposals, solo plans and proactive events.
Person 4's backend should call ``evaluate`` whenever something changes:
a request is added or cancelled, offers are refreshed, or on a timer (e.g. hourly)
so deadline-driven recommendations fire on time.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from . import messages as msg
from .costing import Ctx, OfferBook
from .decisions import recommend_for_group, solo_outlook
from .models import (
    Action,
    BookRequest,
    EngineConfig,
    EngineEvent,
    EngineResult,
    EvaluateInput,
    EventType,
    GroupProposal,
    MerchantOffer,
    MerchantPolicy,
    ParticipantLine,
    ReasonCode,
    SoloPlan,
    SoloStatus,
)
from .money import total
from .optimizer import BlockPlan, optimise


def group_id_for(merchant_id: str, request_ids: list[str]) -> str:
    """Deterministic id: same merchant + same members => same id across evaluations."""
    key = merchant_id + "|" + ",".join(sorted(request_ids))
    return "grp_" + hashlib.sha1(key.encode()).hexdigest()[:10]


class DecisionEngine:
    def __init__(self, config: EngineConfig | None = None):
        self.config = config or EngineConfig()

    # ------------------------------------------------------------------ public

    def evaluate(
        self,
        requests: list[BookRequest],
        offers: list[MerchantOffer],
        merchants: list[MerchantPolicy],
        now: datetime | None = None,
        previous: EngineResult | None = None,
        previous_offers: list[MerchantOffer] | None = None,
    ) -> EngineResult:
        now = now or datetime.now(timezone.utc)
        book = OfferBook(offers, merchants)
        ctx = Ctx(today=now.date(), cfg=self.config, book=book)

        blocks, baselines = optimise(requests, ctx)
        proposals = [self._proposal(b, ctx, baselines, previous_offers) for b in blocks]
        proposals.sort(key=lambda p: (p.recommendation.order_by, p.group_id))

        grouped = {rid for p in proposals for rid in p.request_ids}
        solo_plans = [
            self._solo(r, ctx, baselines.get(r.request_id))
            for r in sorted(requests, key=lambda r: r.request_id)
            if r.request_id not in grouped
        ]

        events = self._events(proposals, solo_plans, previous, {r.request_id: r for r in requests})

        return EngineResult(
            evaluated_at=now,
            proposals=proposals,
            solo_plans=solo_plans,
            events=events,
            total_savings=total(p.total_savings for p in proposals),
            uses_simulated_data=book.uses_simulated_data,
        )

    def evaluate_input(self, data: EvaluateInput) -> EngineResult:
        engine = DecisionEngine(data.config) if data.config else self
        return engine.evaluate(
            data.requests, data.offers, data.merchants, data.now, data.previous, data.previous_offers
        )

    # ------------------------------------------------------------------ builders

    def _proposal(self, b: BlockPlan, ctx: Ctx, baselines, previous_offers) -> GroupProposal:
        gc = b.costing
        pol = ctx.book.policies[gc.merchant_id]
        lines = []
        for r, lc in zip(b.requests, gc.lines):
            base = baselines[r.request_id]
            savings = base.total - lc.total
            lines.append(
                ParticipantLine(
                    request_id=r.request_id,
                    user_id=r.user_id,
                    book_id=r.book_id,
                    title=r.title,
                    quantity=r.quantity,
                    unit_price=lc.unit_price,
                    item_subtotal=lc.item_subtotal,
                    discount=lc.discount,
                    shipping_share=lc.shipping_share,
                    total=lc.total,
                    individual_best_total=base.total,
                    individual_best_merchant=base.merchant_id,
                    individual_meets_deadline=base.meets_deadline,
                    savings=savings,
                    within_budget=r.max_budget is None or lc.total <= r.max_budget,
                    meets_min_savings=savings >= r.min_savings_to_wait,
                    deadline=r.deadline,
                )
            )
        rec = recommend_for_group(b, ctx, previous_offers)
        individual_total = total(l.individual_best_total for l in lines)
        savings = [l.savings for l in lines]
        reasoning = msg.proposal_reasoning(
            pol.name,
            len(lines),
            gc.subtotal - gc.discount_total,
            pol.free_shipping_threshold,
            gc.free_shipping_unlocked,
            min(savings),
            max(savings),
            gc.expected_delivery,
            min(r.deadline for r in b.requests),
        )
        if gc.discount_total > 0:
            reasoning.insert(1, f"Volume discount of {gc.discount_pct}% applied for {gc.item_count} books.")
        reasoning.append(rec.explanation)
        return GroupProposal(
            group_id=group_id_for(gc.merchant_id, [r.request_id for r in b.requests]),
            merchant_id=gc.merchant_id,
            merchant_name=pol.name,
            pickup_point=b.requests[0].pickup_point,
            participants=lines,
            item_count=gc.item_count,
            subtotal=gc.subtotal,
            discount_total=gc.discount_total,
            shipping_fee=gc.shipping_fee,
            total=gc.total,
            individual_total=individual_total,
            total_savings=total(savings),
            free_shipping_unlocked=gc.free_shipping_unlocked,
            amount_to_free_shipping=gc.amount_to_free_shipping,
            expected_delivery=gc.expected_delivery,
            recommendation=rec,
            reasoning=reasoning,
            is_simulated=ctx.book.uses_simulated_data,
        )

    def _solo(self, r: BookRequest, ctx: Ctx, best) -> SoloPlan:
        status, potential, order_by, reasons = solo_outlook(r, best, ctx)
        return SoloPlan(
            request_id=r.request_id,
            user_id=r.user_id,
            status=SoloStatus(status),
            best_merchant=best.merchant_id if best else None,
            best_total=best.total if best else None,
            best_expected_delivery=best.expected_delivery if best else None,
            potential_group_saving=potential,
            order_by=order_by,
            reasoning=reasons,
        )

    # ------------------------------------------------------------------ events

    def _events(
        self,
        proposals: list[GroupProposal],
        solos: list[SoloPlan],
        previous: EngineResult | None,
        reqs: dict[str, BookRequest],
    ) -> list[EngineEvent]:
        prev_groups = {p.group_id: p for p in (previous.proposals if previous else [])}
        prev_solos = {s.request_id: s for s in (previous.solo_plans if previous else [])}
        new_by_req = {rid: p.group_id for p in proposals for rid in p.request_ids}
        events: list[EngineEvent] = []

        def ev(t: EventType, group: GroupProposal | None, rids: list[str], text: str,
               key_suffix: str = "", decision: bool = False, **data) -> None:
            gid = group.group_id if group else None
            users = sorted({reqs[r].user_id for r in rids if r in reqs})
            events.append(
                EngineEvent(
                    event_type=t,
                    dedupe_key=f"{t.value}:{gid or ','.join(sorted(rids))}{(':' + key_suffix) if key_suffix else ''}",
                    group_id=gid,
                    request_ids=sorted(rids),
                    user_ids=users,
                    message=text,
                    data=data,
                    needs_user_decision=decision,
                )
            )

        for p in proposals:
            members = set(p.request_ids)
            touching = [pg for pg in prev_groups.values() if members & set(pg.request_ids)]
            prev_p = prev_groups.get(p.group_id)

            if prev_p is None:
                if len(touching) >= 2:
                    ev(EventType.GROUPS_MERGED, p, p.request_ids, msg.merged(p, len(touching)),
                       from_groups=[g.group_id for g in touching])
                elif len(touching) == 1:
                    old = set(touching[0].request_ids)
                    if members > old:
                        joined = sorted(members - old)
                        ev(EventType.MEMBERS_JOINED, p, p.request_ids, msg.members_changed(p, True, len(joined)),
                           from_group=touching[0].group_id, joined=joined)
                    elif members < old:
                        left = sorted(old - members)
                        ev(EventType.MEMBERS_LEFT, p, p.request_ids, msg.members_changed(p, False, len(left)),
                           from_group=touching[0].group_id, left=left)
                    else:
                        ev(EventType.GROUP_RESTRUCTURED, p, p.request_ids, msg.restructured(p),
                           from_group=touching[0].group_id)
                else:
                    ev(EventType.GROUP_FORMED, p, p.request_ids, msg.group_formed(p))

            was_unlocked = any(g.free_shipping_unlocked for g in touching)
            if p.free_shipping_unlocked and not was_unlocked:
                ev(EventType.FREE_SHIPPING_UNLOCKED, p, p.request_ids, msg.free_shipping(p))

            if ReasonCode.PRICE_DROP in p.recommendation.reasons:
                ev(EventType.PRICE_DROP, p, p.request_ids, msg.price_drop(p),
                   key_suffix=p.recommendation.order_by.isoformat())

            already_recommended = prev_p is not None and prev_p.recommendation.action is Action.BUY_NOW
            if p.recommendation.action is Action.BUY_NOW and not already_recommended:
                ev(EventType.BUY_NOW_RECOMMENDED, p, p.request_ids, msg.buy_now(p), decision=True,
                   reasons=[r.value for r in p.recommendation.reasons],
                   order_by=p.recommendation.order_by.isoformat())

        # previous groups that were split or dissolved
        for pg in prev_groups.values():
            if any(p.group_id == pg.group_id for p in proposals):
                continue
            destinations = {new_by_req.get(rid) for rid in pg.request_ids}
            grouped_dest = {d for d in destinations if d is not None}
            if not grouped_dest:
                ev(EventType.GROUP_DISSOLVED, None, pg.request_ids, msg.dissolved(),
                   key_suffix=pg.group_id, from_group=pg.group_id)
            elif len(grouped_dest) >= 2:
                ev(EventType.GROUP_SPLIT, None, pg.request_ids, msg.split(len(grouped_dest)),
                   key_suffix=pg.group_id, from_group=pg.group_id, into_groups=sorted(grouped_dest))

        # solo status changes worth telling the user about
        notify = {
            SoloStatus.BUY_SOLO_RECOMMENDED: (EventType.SOLO_BUY_RECOMMENDED, True),
            SoloStatus.DEADLINE_UNREACHABLE: (EventType.DEADLINE_UNREACHABLE, True),
            SoloStatus.UNSERVABLE: (EventType.REQUEST_UNSERVABLE, False),
        }
        for s in solos:
            if s.status not in notify:
                continue
            prev_s = prev_solos.get(s.request_id)
            if prev_s is not None and prev_s.status == s.status:
                continue
            t, decision = notify[s.status]
            ev(t, None, [s.request_id], msg.solo(s), decision=decision,
               best_merchant=s.best_merchant, best_total=str(s.best_total) if s.best_total else None)

        return events
