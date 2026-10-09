"""Person 4 standalone demonstration. Requests/offers/proposal are mock teammate inputs."""
import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--scenario", choices=["success", "decline", "timeout", "refund", "withdraw"], default="success")
    args = parser.parse_args()
    keyfile = Path(__file__).resolve().parents[1] / "data" / "service.key"
    key = os.getenv("BOOKPOOL_SERVICE_API_KEY") or (keyfile.read_text().strip() if keyfile.exists() else "")
    if not key:
        raise SystemExit("Start run_local.py first or set BOOKPOOL_SERVICE_API_KEY")
    now = datetime.now(timezone.utc)
    expiry = (now + timedelta(minutes=20)).isoformat()
    delivery = (now + timedelta(days=3)).isoformat()
    deadline = (now + timedelta(days=7)).isoformat()
    run = uuid.uuid4().hex[:10]
    with httpx.Client(base_url=args.url, headers={"Authorization": "Bearer " + key}, timeout=30) as client:
        health = client.get("/health").json()
        if health["payment_mode"] != "mock":
            raise SystemExit("Demo is mock-only; it never contacts Reap or uses your test card")
        def post(path, data=None):
            response = client.post(path, json=data)
            response.raise_for_status()
            return response.json()
        users, allocations = [], []
        titles = ["Atomic Habits", "Deep Work", "The Psychology of Money"]
        for i, (name, title) in enumerate(zip(["Alice", "Bob", "Charlie"], titles)):
            user = post("/v1/users", {"telegram_id": f"demo-{run}-{i}", "display_name": name})
            users.append(user)
            request = post("/v1/requests", {"user_id": user["id"], "title": title,
                                          "budget_minor": 3500, "min_savings_minor": 100,
                                          "latest_delivery_at": deadline, "pickup_location": "NTU shared collection point"})
            offer = post("/v1/offers", {"request_id": request["id"], "merchant": "kinokuniya.com.sg",
                                      "variant_id": f"MOCK-{i}", "unit_price_minor": 2500,
                                      "individual_total_minor": 3100, "delivery_at": delivery,
                                      "expires_at": expiry, "source": "mock"})
            allocations.append({"request_id": request["id"], "offer_id": offer["id"], "amount_minor": 2500})
        group = post("/v1/groups", {"client_reference": "demo-" + run, "merchant": "kinokuniya.com.sg",
                                   "purchaser_id": users[0]["id"], "total_minor": 7500,
                                   "expires_at": expiry, "delivery_at": delivery,
                                   "pickup_location": "NTU shared collection point",
                                   "reason": "Illustrative demo: free delivery is met; recommend buying now.",
                                   "allocations": allocations})
        print("Mock prices only: individual S$93.00 → shared S$75.00; group savings S$18.00")
        print("Group:", group["id"])
        for user in users:
            path = f'/v1/groups/{group["id"]}/participants/{user["id"]}'
            post(path + "/approve", {"version": 1})
            post(path + "/authorize", {"version": 1})
            print(user["display_name"], "approved S$25.00 and authorized a simulated contribution")
        if args.scenario == "withdraw":
            result = post(f'/v1/groups/{group["id"]}/participants/{users[0]["id"]}/withdraw', {"version": 1})
            print("Group state:", result["state"], "— simulated holds voided")
            return
        result = post(f'/v1/groups/{group["id"]}/execute', {
            "version": 1, "outcome": args.scenario if args.scenario in {"decline", "timeout"} else "success"})
        print("Order:", result["id"], "state:", result["state"])
        if args.scenario == "timeout":
            result = post(f'/v1/orders/{result["id"]}/reconcile')
            print("Reconciled state:", result["state"])
        if args.scenario == "refund":
            result = post(f'/v1/orders/{result["id"]}/refund', {"reason": "Demonstrate simulated cancellation and refund"})
            print("Refund state:", result["state"])
        print("Merchant reference:", result.get("merchant_order_id"))
        print("No real funds or merchant order were created.")


if __name__ == "__main__":
    main()
