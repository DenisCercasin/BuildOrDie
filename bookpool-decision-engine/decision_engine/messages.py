"""Template text for proposals and events.

Deterministic so the numbers shown to users always match the engine. Person 1 may
pass these through the LLM for tone, but should never let it change the figures.
"""

from __future__ import annotations

from .models import Action, GroupProposal, ParticipantLine, SoloPlan
from .money import fmt


def _name(p: ParticipantLine) -> str:
    return p.title or p.book_id


def participant_summary(proposal: GroupProposal, request_id: str) -> str:
    """A per-user summary of a group proposal, for the approval message."""
    line = next(p for p in proposal.participants if p.request_id == request_id)
    others = len(proposal.participants) - 1
    ship = "free delivery" if proposal.free_shipping_unlocked else f"your delivery share {fmt(line.shipping_share)}"
    txt = (
        f"📚 {_name(line)} via {proposal.merchant_name}, with {others} other buyer"
        f"{'s' if others != 1 else ''}.\n"
        f"You pay {fmt(line.total)} ({fmt(line.item_subtotal - line.discount)} book + {ship}).\n"
        f"Alone it would cost {fmt(line.individual_best_total)} — you save {fmt(line.savings)}.\n"
        f"Pickup: {proposal.pickup_point}, expected {proposal.expected_delivery:%a %d %b}."
    )
    if proposal.is_simulated:
        txt += "\n(Prices are simulated for this demo.)"
    return txt


def group_formed(p: GroupProposal) -> str:
    return (
        f"We found {len(p.participants)} buyers who can order together from {p.merchant_name}, "
        f"saving {fmt(p.total_savings)} in total. {p.recommendation.explanation}"
    )


def members_changed(p: GroupProposal, joined: bool, n: int) -> str:
    verb = "joined" if joined else "left"
    return (
        f"{n} buyer{'s' if n != 1 else ''} {verb} your {p.merchant_name} group. "
        f"It now has {len(p.participants)} buyers and saves {fmt(p.total_savings)} in total."
    )


def merged(p: GroupProposal, n_groups: int) -> str:
    return (
        f"We combined {n_groups} groups into one {p.merchant_name} order of "
        f"{len(p.participants)} buyers — total savings now {fmt(p.total_savings)}."
    )


def split(n_parts: int) -> str:
    return (
        f"Your group has been reorganised into {n_parts} orders because that's cheaper or "
        f"fits everyone's deadlines better. Check your new proposal."
    )


def restructured(p: GroupProposal) -> str:
    return f"Your group changed. It's now {len(p.participants)} buyers at {p.merchant_name}."


def dissolved() -> str:
    return "Your group no longer saves anyone money (prices, stock or members changed). We'll keep looking."


def free_shipping(p: GroupProposal) -> str:
    lo = min(x.savings for x in p.participants)
    hi = max(x.savings for x in p.participants)
    saving = fmt(lo) if lo == hi else f"{fmt(lo)}–{fmt(hi)}"
    return f"Great news! Your group unlocked free delivery at {p.merchant_name}. Each of you is saving {saving}."


def buy_now(p: GroupProposal) -> str:
    lo = min(x.savings for x in p.participants)
    hi = max(x.savings for x in p.participants)
    saving = fmt(lo) if lo == hi else f"{fmt(lo)}–{fmt(hi)}"
    return (
        f"You're saving {saving} each compared with buying individually. "
        f"{p.recommendation.explanation} We recommend ordering now. Proceed or keep waiting?"
    )


def price_drop(p: GroupProposal) -> str:
    return f"A price in your {p.merchant_name} order just dropped. Good moment to buy."


def solo(plan: SoloPlan) -> str:
    return " ".join(plan.reasoning)


def proposal_reasoning(p_merchant: str, n: int, subtotal_after_discount, threshold, unlocked: bool,
                       savings_lo, savings_hi, eta, tightest_deadline) -> list[str]:
    out = [f"Combined {n} orders at {p_merchant} ({fmt(subtotal_after_discount)} of books)."]
    if threshold is not None:
        if unlocked:
            out.append(f"That clears the {fmt(threshold)} free-delivery threshold.")
        else:
            out.append(
                f"{fmt(threshold - subtotal_after_discount)} short of free delivery; "
                f"the delivery fee is shared."
            )
    rng = fmt(savings_lo) if savings_lo == savings_hi else f"{fmt(savings_lo)} to {fmt(savings_hi)}"
    out.append(f"Each participant saves {rng} versus their cheapest on-time solo option.")
    out.append(f"Expected delivery {eta:%d %b}; tightest deadline {tightest_deadline:%d %b}.")
    return out


def action_label(a: Action) -> str:
    return "Order now" if a is Action.BUY_NOW else "Keep waiting"
