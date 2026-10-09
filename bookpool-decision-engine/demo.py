"""Walk through the brief's demo scenario with the decision engine alone.

    python demo.py

Alice requests -> Bob joins -> Charlie joins (free delivery) -> buy-now recommended.
Uses SIMULATED merchant data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from decision_engine import DecisionEngine, participant_summary
from decision_engine import mock_data as mock
from decision_engine.money import fmt

SGT = timezone(timedelta(hours=8))


def show(step: str, result) -> None:
    print(f"\n{'=' * 72}\n{step}\n{'=' * 72}")
    for p in result.proposals:
        rec = p.recommendation
        print(f"GROUP {p.group_id} @ {p.merchant_name} — {len(p.participants)} buyers, "
              f"total {fmt(p.total)} vs {fmt(p.individual_total)} alone (saves {fmt(p.total_savings)})")
        for line in p.participants:
            print(f"   {line.user_id:<11} {line.title:<26} pays {fmt(line.total):>8}  "
                  f"(alone {fmt(line.individual_best_total)}, saves {fmt(line.savings)})")
        print(f"   → {rec.action.value}  [{', '.join(r.value for r in rec.reasons)}]  order by {rec.order_by}")
        for r in p.reasoning:
            print(f"     · {r}")
    for s in result.solo_plans:
        print(f"SOLO {s.user_id}: {s.status.value} — best {s.best_merchant} {fmt(s.best_total) if s.best_total else '-'}")
        for r in s.reasoning:
            print(f"     · {r}")
    if result.events:
        print("\nEVENTS (proactive messages for the bot):")
        for e in result.events:
            flag = " [needs decision]" if e.needs_user_decision else ""
            print(f" ▸ {e.event_type.value} → {', '.join(e.user_ids)}{flag}\n   {e.message}")


def main() -> None:
    engine = DecisionEngine()
    now = datetime(2026, 10, 9, 18, 0, tzinfo=SGT)
    offers = mock.offers(now)
    reqs = mock.demo_requests(now.date())

    r1 = engine.evaluate([reqs["alice"]], offers, mock.MERCHANTS, now)
    show("1. Alice requests Atomic Habits", r1)

    r2 = engine.evaluate([reqs["alice"], reqs["bob"]], offers, mock.MERCHANTS, now, previous=r1)
    show("2. Bob requests Deep Work — engine combines the two orders", r2)

    r3 = engine.evaluate(list(reqs.values()), offers, mock.MERCHANTS, now, previous=r2)
    show("3. Charlie requests The Psychology of Money — free delivery unlocked", r3)

    p = r3.proposals[0]
    print(f"\n{'=' * 72}\n4. Approval message Alice would see\n{'=' * 72}")
    print(participant_summary(p, "req_alice"))
    print(f"\nProposal JSON for the backend (truncated):\n{p.model_dump_json(indent=2)[:700]}…")


if __name__ == "__main__":
    main()
