from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from decision_engine import (
    Action,
    DecisionEngine,
    EngineConfig,
    EngineResult,
    EventType,
    ReasonCode,
    SoloStatus,
)
from decision_engine import mock_data as mock
from decision_engine.money import split_amount

SGT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 9, 18, 0, tzinfo=SGT)
TODAY = NOW.date()
AH, DW, PM, TFS, SAP = (
    "9781847941831",
    "9780349411903",
    "9780857197689",
    "9780141983479",
    "9780062316097",
)


@pytest.fixture
def engine() -> DecisionEngine:
    return DecisionEngine()


@pytest.fixture
def offers():
    return mock.offers(NOW)


def run(engine, reqs, offers=None, previous=None, previous_offers=None, now=NOW):
    return engine.evaluate(reqs, offers or mock.offers(now), mock.MERCHANTS, now, previous, previous_offers)


def types(result: EngineResult) -> list[EventType]:
    return [e.event_type for e in result.events]


# ------------------------------------------------------------------ money


def test_split_amount_sums_exactly():
    parts = split_amount(D("4.99"), [D(1)] * 3)
    assert sum(parts) == D("4.99")
    assert parts == [D("1.67"), D("1.66"), D("1.66")]
    assert sum(split_amount(D("10.00"), [D("26.00"), D("24.50"), D("0")])) == D("10.00")


# ------------------------------------------------------------------ demo scenario


def test_demo_flow(engine):
    reqs = mock.demo_requests(TODAY)

    r1 = run(engine, [reqs["alice"]])
    assert not r1.proposals
    assert r1.solo_plans[0].status is SoloStatus.WAITING_FOR_GROUP
    assert r1.solo_plans[0].best_total == D("30.99")

    r2 = run(engine, [reqs["alice"], reqs["bob"]], previous=r1)
    assert len(r2.proposals) == 1
    g2 = r2.proposals[0]
    assert g2.merchant_id == mock.POPULAR
    assert not g2.free_shipping_unlocked
    assert g2.recommendation.action is Action.WAIT
    assert types(r2) == [EventType.GROUP_FORMED]

    r3 = run(engine, list(reqs.values()), previous=r2)
    g3 = r3.proposals[0]
    assert len(g3.participants) == 3
    assert g3.free_shipping_unlocked
    assert g3.total == D("73.40")
    assert all(p.savings == D("4.99") for p in g3.participants)
    assert g3.recommendation.action is Action.BUY_NOW
    assert ReasonCode.LOW_MARGINAL_VALUE in g3.recommendation.reasons
    assert set(types(r3)) == {
        EventType.MEMBERS_JOINED,
        EventType.FREE_SHIPPING_UNLOCKED,
        EventType.BUY_NOW_RECOMMENDED,
    }
    buy = next(e for e in r3.events if e.event_type is EventType.BUY_NOW_RECOMMENDED)
    assert buy.needs_user_decision and buy.user_ids == ["tg_alice", "tg_bob", "tg_charlie"]

    # re-evaluating with nothing changed must not re-send notifications
    r4 = run(engine, list(reqs.values()), previous=r3)
    assert r4.events == []
    assert r4.proposals[0].group_id == g3.group_id


def test_group_totals_are_consistent(engine):
    r = run(engine, list(mock.demo_requests(TODAY).values()))
    for p in r.proposals:
        assert sum(l.total for l in p.participants) == p.total
        assert sum(l.shipping_share for l in p.participants) == p.shipping_fee
        assert p.individual_total - p.total == p.total_savings


# ------------------------------------------------------------------ constraints


def test_different_pickup_points_never_grouped(engine):
    a = mock.request("a", "u1", AH, TODAY, 10)
    b = mock.request("b", "u2", DW, TODAY, 10, pickup_point=mock.PICKUP_NUS)
    r = run(engine, [a, b])
    assert r.proposals == []


def test_deadline_excludes_slow_merchant(engine):
    # POPULAR delivers in 4 days (+1 processing) = day 5; deadline day 4 forces Kinokuniya
    a = mock.request("a", "u1", AH, TODAY, 4)
    b = mock.request("b", "u2", DW, TODAY, 4)
    c = mock.request("c", "u3", PM, TODAY, 4)
    r = run(engine, [a, b, c])
    assert len(r.proposals) == 1
    p = r.proposals[0]
    assert p.merchant_id == mock.KINO
    assert all(p.expected_delivery <= l.deadline for l in p.participants)


def test_budget_respected(engine):
    a = mock.request("a", "u1", AH, TODAY, 10, max_budget=D("27.00"))  # can't afford 26.00 + 2.50 share
    b = mock.request("b", "u2", DW, TODAY, 10)
    r = run(engine, [a, b])
    for p in r.proposals:
        for l in p.participants:
            assert l.within_budget


def test_nobody_worse_off_with_proportional_split():
    eng = DecisionEngine(EngineConfig(cost_split="proportional"))
    reqs = [mock.request(f"r{i}", f"u{i}", b, TODAY, 12) for i, b in enumerate([AH, DW, PM, TFS, SAP])]
    r = run(eng, reqs)
    for p in r.proposals:
        assert all(l.savings >= D("0.01") for l in p.participants)


def test_max_group_size_splits_into_smaller_groups():
    eng = DecisionEngine(EngineConfig(max_group_size=2))
    reqs = [mock.request(f"r{i}", f"u{i}", b, TODAY, 12) for i, b in enumerate([AH, DW, PM, SAP])]
    r = run(eng, reqs)
    assert len(r.proposals) == 2
    assert all(len(p.participants) == 2 for p in r.proposals)


def test_same_book_respects_stock():
    eng = DecisionEngine()
    # POPULAR has 3 Sapiens; 4 people want it -> POPULAR can't take all four
    reqs = [mock.request(f"r{i}", f"u{i}", SAP, TODAY, 12) for i in range(4)]
    r = run(eng, reqs)
    for p in r.proposals:
        if p.merchant_id == mock.POPULAR:
            assert p.item_count <= 3


# ------------------------------------------------------------------ decisions


def test_deadline_approaching_triggers_buy_now(engine):
    # deadline day 6 at POPULAR (4 days + 1 processing) => order by tomorrow
    a = mock.request("a", "u1", AH, TODAY, 6)
    b = mock.request("b", "u2", DW, TODAY, 6)
    r = run(engine, [a, b])
    p = r.proposals[0]
    assert p.recommendation.action is Action.BUY_NOW
    assert ReasonCode.DEADLINE_APPROACHING in p.recommendation.reasons


def test_low_stock_triggers_buy_now(engine):
    offers = mock.offers(NOW, overrides={(mock.POPULAR, AH): {"stock_qty": 2}})
    a = mock.request("a", "u1", AH, TODAY, 12)
    b = mock.request("b", "u2", DW, TODAY, 12)
    r = run(engine, [a, b], offers=offers)
    p = r.proposals[0]
    assert p.merchant_id == mock.POPULAR
    assert ReasonCode.LOW_STOCK in p.recommendation.reasons
    assert p.recommendation.action is Action.BUY_NOW


def test_price_drop_event(engine):
    a = mock.request("a", "u1", AH, TODAY, 12)
    b = mock.request("b", "u2", DW, TODAY, 12)
    before = mock.offers(NOW)
    r1 = run(engine, [a, b], offers=before)
    after = mock.offers(NOW, overrides={(mock.POPULAR, AH): {"price": D("22.00")}})
    r2 = run(engine, [a, b], offers=after, previous=r1, previous_offers=before)
    assert EventType.PRICE_DROP in types(r2)
    assert r2.proposals[0].recommendation.action is Action.BUY_NOW


def test_volume_discount_tier_applied():
    # 5 books at Kinokuniya on a tight deadline unlock the 5% tier
    eng = DecisionEngine()
    reqs = [mock.request(f"r{i}", f"u{i}", b, TODAY, 4) for i, b in enumerate([AH, DW, PM, TFS, SAP])]
    r = run(eng, reqs)
    p = r.proposals[0]
    assert p.merchant_id == mock.KINO and p.item_count == 5
    assert p.discount_total > 0


# ------------------------------------------------------------------ solo outcomes


def test_solo_buy_when_saving_below_users_threshold(engine):
    a = mock.request("a", "u1", AH, TODAY, 12, min_savings_to_wait=D("10"))
    r = run(engine, [a])
    s = r.solo_plans[0]
    assert s.status is SoloStatus.BUY_SOLO_RECOMMENDED
    assert types(r) == [EventType.SOLO_BUY_RECOMMENDED]


def test_unservable_and_unreachable(engine):
    a = mock.request("a", "u1", "0000000000000", TODAY, 12)
    b = mock.request("b", "u2", AH, TODAY, 2)  # fastest is 3+1 days
    r = run(engine, [a, b])
    st = {s.request_id: s.status for s in r.solo_plans}
    assert st == {"a": SoloStatus.UNSERVABLE, "b": SoloStatus.DEADLINE_UNREACHABLE}


def test_unapproved_merchant_ignored(engine):
    merchants = [m.model_copy(update={"approved": False}) if m.merchant_id == mock.POPULAR else m
                 for m in mock.MERCHANTS]
    reqs = list(mock.demo_requests(TODAY).values())
    r = engine.evaluate(reqs, mock.offers(NOW), merchants, NOW)
    assert all(p.merchant_id == mock.KINO for p in r.proposals)
    assert all(s.best_merchant in (None, mock.KINO) for s in r.solo_plans)


# ------------------------------------------------------------------ restructuring events


def test_groups_merged_event(engine):
    a, b = mock.request("a", "u1", AH, TODAY, 12), mock.request("b", "u2", DW, TODAY, 12)
    c, d = mock.request("c", "u3", TFS, TODAY, 12), mock.request("d", "u4", SAP, TODAY, 12)
    r_ab, r_cd = run(engine, [a, b]), run(engine, [c, d])
    previous = r_ab.model_copy(update={"proposals": r_ab.proposals + r_cd.proposals, "solo_plans": []})
    r = run(engine, [a, b, c, d], previous=previous)
    assert len(r.proposals) == 1
    assert EventType.GROUPS_MERGED in types(r)


def test_group_dissolved_when_member_cancels(engine):
    a, b = mock.request("a", "u1", AH, TODAY, 12), mock.request("b", "u2", DW, TODAY, 12)
    r1 = run(engine, [a, b])
    r2 = run(engine, [a], previous=r1)
    assert r2.proposals == []
    assert EventType.GROUP_DISSOLVED in types(r2)


def test_members_left_event(engine):
    reqs = list(mock.demo_requests(TODAY).values())
    r1 = run(engine, reqs)
    r2 = run(engine, reqs[:2], previous=r1)
    assert EventType.MEMBERS_LEFT in types(r2)


# ------------------------------------------------------------------ solver quality


def _random_requests(rng, n):
    books = [AH, DW, PM, TFS, SAP]
    return [
        mock.request(f"r{i}", f"u{i}", rng.choice(books), TODAY, rng.randint(4, 14))
        for i in range(n)
    ]


@pytest.mark.parametrize("seed", range(8))
def test_greedy_feasible_and_close_to_exact(seed):
    rng = random.Random(seed)
    reqs = _random_requests(rng, 7)
    exact = run(DecisionEngine(), reqs)
    greedy = run(DecisionEngine(EngineConfig(exact_solver_max_requests=2)), reqs)
    assert greedy.total_savings <= exact.total_savings
    assert greedy.total_savings >= exact.total_savings * D("0.8")
    for res in (exact, greedy):
        seen = set()
        for p in res.proposals:
            assert not (set(p.request_ids) & seen)
            seen |= set(p.request_ids)
            assert all(l.savings > 0 and p.expected_delivery <= l.deadline for l in p.participants)


def test_deterministic(engine):
    reqs = _random_requests(random.Random(42), 9)
    a, b = run(engine, reqs), run(engine, list(reversed(reqs)))
    assert [p.group_id for p in a.proposals] == [p.group_id for p in b.proposals]
    assert a.total_savings == b.total_savings


def test_large_pool_uses_greedy_quickly():
    import time

    reqs = _random_requests(random.Random(1), 40)
    t = time.perf_counter()
    r = run(DecisionEngine(), reqs)
    assert time.perf_counter() - t < 20
    assert r.total_savings > 0


# ------------------------------------------------------------------ API


def test_api_roundtrip():
    from fastapi.testclient import TestClient

    from decision_engine.api import app

    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    body = {
        "requests": [r.model_dump(mode="json") for r in mock.demo_requests(TODAY).values()],
        "offers": [o.model_dump(mode="json") for o in mock.offers(NOW)],
        "merchants": [m.model_dump(mode="json") for m in mock.MERCHANTS],
        "now": NOW.isoformat(),
    }
    res = client.post("/v1/evaluate", json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["proposals"][0]["recommendation"]["action"] == "BUY_NOW"
    assert data["proposals"][0]["total"] == "73.40"
    # previous result round-trips through JSON
    body["previous"] = data
    res2 = client.post("/v1/evaluate", json=body)
    assert res2.json()["events"] == []
