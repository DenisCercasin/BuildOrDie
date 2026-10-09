"""Expire stale commitments and reconcile pending provider checkouts."""
import os
import time
import httpx


def main():
    key = os.environ["BOOKPOOL_SERVICE_API_KEY"]
    url = os.getenv("BACKEND_URL", "http://localhost:8000")
    with httpx.Client(base_url=url, headers={"Authorization": "Bearer " + key}, timeout=30) as client:
        while True:
            try:
                client.post("/v1/maintenance/expire").raise_for_status()
                response = client.get("/v1/groups", params={"limit": 500})
                response.raise_for_status()
                for group in response.json():
                    if group["state"] in {"CHECKOUT_PENDING", "PAYMENT_UNKNOWN", "PAYMENT_PENDING", "AWAITING_PAYMENT_APPROVAL"}:
                        order = group.get("order")
                        if order and order.get("error_code") != "MANUAL_RECONCILIATION_REQUIRED":
                            client.post(f'/v1/orders/{order["id"]}/reconcile').raise_for_status()
            except httpx.HTTPError:
                print("Backend temporarily unavailable; reconciliation will retry.", flush=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
