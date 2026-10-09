from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import copy

from sqlalchemy import select
from app.db import Group, Order, Participant
from app.main import allocate
from conftest import KEY, approve_all, dt, setup_group


def test_full_demo_refund_and_duplicate_execution(client):
    users, reqs, _, _, group = setup_group(client)
    approve_all(client, users, group)
    path = f'/v1/groups/{group["id"]}/execute'
    order = client.post(path, json={"version": 1}).json()
    assert order["state"] == "ORDERED"
    assert order["simulated"] is True
    assert order["final_minor"] == 7500
    assert client.post(path, json={"version": 1}).json()["id"] == order["id"]
    assert all(r["status"] == "ORDERED" for r in client.get("/v1/requests").json())
    refunded = client.post(f'/v1/orders/{order["id"]}/refund', json={"reason": "Demo cancellation"}).json()
    assert refunded["state"] == "REFUNDED"
    ledger = client.get(f'/v1/payments?group_id={group["id"]}').json()
    assert sum(t["amount_minor"] for t in ledger if t["kind"] == "CAPTURE") == 7500
    assert sum(t["amount_minor"] for t in ledger if t["kind"] == "REFUND") == 7500
    assert client.post(f'/v1/orders/{order["id"]}/refund', json={"reason": "Duplicate"}).json()["id"] == order["id"]
    assert all(r["status"] == "OPEN" and r["group_id"] is None for r in client.get("/v1/requests").json())
    events = client.get("/v1/events").json()
    assert "ORDER_PLACED" in [e["kind"] for e in events["events"]]
    assert client.get(f'/v1/events?after={events["next_cursor"]}').json()["events"] == []


def test_missing_commitment_and_declined_contribution(client):
    users, _, _, _, group = setup_group(client)
    approve_all(client, users[:2], group)
    path = f'/v1/groups/{group["id"]}/execute'
    assert client.post(path, json={"version": 1}).status_code == 409
    member = f'/v1/groups/{group["id"]}/participants/{users[2]["id"]}'
    client.post(member + "/approve", json={"version": 1})
    client.post(member + "/authorize", json={"version": 1, "outcome": "decline"})
    assert client.post(path, json={"version": 1}).status_code == 409
    client.post(member + "/authorize", json={"version": 1, "outcome": "success"})
    assert client.post(path, json={"version": 1}).json()["state"] == "ORDERED"


def test_revision_invalidates_old_buttons_and_authorizations(client):
    users, _, _, proposal, group = setup_group(client)
    approve_all(client, users, group)
    proposal["total_minor"] = 7800
    for a in proposal["allocations"]:
        a["amount_minor"] = 2600
    response = client.put(f'/v1/groups/{group["id"]}/proposal?expected_version=1', json=proposal)
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised["version"] == 2
    assert all(p["approved_version"] is None and p["payment_state"] == "PENDING" for p in revised["participants"])
    stale = client.post(f'/v1/groups/{group["id"]}/participants/{users[0]["id"]}/approve', json={"version": 1})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "STALE_PROPOSAL"
    assert client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 2}).status_code == 409


def test_budget_savings_and_deadline_validation(client):
    _, _, _, proposal, group = setup_group(client)
    for mutation, code in [
        (lambda p: (p.update(total_minor=10800), [a.update(amount_minor=3600) for a in p["allocations"]]), "BUDGET_EXCEEDED"),
        (lambda p: (p.update(total_minor=9150), [a.update(amount_minor=3050) for a in p["allocations"]]), "MINIMUM_SAVINGS"),
        (lambda p: p.update(delivery_at=dt(days=10)), "DELIVERY_DEADLINE"),
        (lambda p: p.update(pickup_location="Other campus"), "PICKUP_MISMATCH"),
    ]:
        payload = copy.deepcopy(proposal)
        mutation(payload)
        response = client.put(f'/v1/groups/{group["id"]}/proposal?expected_version=1', json=payload)
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == code
    assert client.get(f'/v1/groups/{group["id"]}').json()["version"] == 1


def test_idempotent_proposal_and_conflicting_reservation(client):
    _, _, _, proposal, group = setup_group(client)
    assert client.post("/v1/groups", json=proposal).json()["id"] == group["id"]
    modified = copy.deepcopy(proposal)
    modified["reason"] = "Changed content"
    assert client.post("/v1/groups", json=modified).json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    modified["client_reference"] = "another-group"
    assert client.post("/v1/groups", json=modified).json()["detail"]["code"] == "REQUEST_RESERVED"


def test_withdrawal_voids_holds_and_releases_requests(client):
    users, _, _, _, group = setup_group(client)
    approve_all(client, users, group)
    response = client.post(f'/v1/groups/{group["id"]}/participants/{users[0]["id"]}/withdraw', json={"version": 1})
    assert response.json()["state"] == "CANCELLED"
    assert all(p["payment_state"] == "VOIDED" for p in response.json()["participants"])
    assert all(r["group_id"] is None for r in client.get("/v1/requests").json())
    assert client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 1}).status_code == 409


def test_timeout_does_not_allow_cancel_or_second_order(client):
    users, _, _, _, group = setup_group(client)
    approve_all(client, users, group)
    order = client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 1, "outcome": "timeout"}).json()
    assert order["state"] == "PAYMENT_UNKNOWN"
    assert client.post(f'/v1/groups/{group["id"]}/cancel', json={"reason": "Unknown payment"}).status_code == 409
    retry = client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 1}).json()
    assert retry["id"] == order["id"] and retry["state"] == "PAYMENT_UNKNOWN"
    assert client.post(f'/v1/orders/{order["id"]}/reconcile').json()["state"] == "ORDERED"


def test_declined_checkout_releases_holds(client):
    users, _, _, _, group = setup_group(client)
    approve_all(client, users, group)
    order = client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 1, "outcome": "decline"}).json()
    assert order["state"] == "FAILED"
    assert all(r["group_id"] is None for r in client.get("/v1/requests").json())
    assert client.post(f'/v1/orders/{order["id"]}/refund', json={"reason": "No charge"}).status_code == 409


def test_expiry_prevents_execution_and_voids_holds(client):
    users, _, _, _, group = setup_group(client)
    approve_all(client, users, group)
    with client.app.state.db.transaction(write=True) as s:
        g = s.get(Group, group["id"])
        g.expires_at = dt(minutes=-1)
    assert client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 1}).status_code == 409
    expired = client.post("/v1/maintenance/expire").json()
    assert expired["expired_group_ids"] == [group["id"]]
    assert client.get(f'/v1/groups/{group["id"]}').json()["state"] == "EXPIRED"


def test_identity_is_enforced_and_tokens_not_in_views(client):
    users, _, _, _, group = setup_group(client)
    alice_headers = {"Authorization": "Bearer " + users[0]["access_token"]}
    assert client.post(f'/v1/groups/{group["id"]}/participants/{users[1]["id"]}/approve',
                       json={"version": 1}, headers=alice_headers).status_code == 403
    assert client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 1}, headers=alice_headers).status_code == 403
    stranger = client.post("/v1/users", json={"telegram_id": "outsider", "display_name": "Other"}).json()
    assert client.get(f'/v1/groups/{group["id"]}', headers={"Authorization": "Bearer " + stranger["access_token"]}).status_code == 403
    assert client.get("/v1/requests", headers={"Authorization": ""}).status_code == 401
    view = client.get(f'/v1/groups/{group["id"]}').text
    assert "token_hash" not in view and "shipping_address" not in view
    assert users[0]["access_token"] not in view


def test_concurrent_checkout_creates_only_one_order(client):
    users, _, _, _, group = setup_group(client)
    approve_all(client, users, group)
    def execute(_):
        return client.post(f'/v1/groups/{group["id"]}/execute', json={"version": 1}).json()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(execute, range(4)))
    assert len({r["id"] for r in results}) == 1
    with client.app.state.db.transaction() as s:
        assert len(list(s.scalars(select(Order)))) == 1


def test_cent_allocation_and_non_integer_money_rejected(client):
    assert sum(allocate(10001, [3333, 3333, 3334])) == 10001
    assert allocate(5, [1, 1, 1]) == [2, 2, 1]
    response = client.post("/v1/requests", json={"user_id": "x", "title": "Book", "budget_minor": 35.5,
                           "latest_delivery_at": dt(days=7), "pickup_location": "NTU"})
    assert response.status_code == 422
