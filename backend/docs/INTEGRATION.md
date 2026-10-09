# Contract for Persons 1–3

Base path: `/v1`. All endpoints except `/health`, the landing page and the payment-return page require `Authorization: Bearer <token>`. Use the shared service key only from trusted servers. Individual user tokens may access their own records/commitments but cannot write offers, proposals, execute orders or read the global event feed.

Money uses integer cents: `2500` = S$25.00. All timestamps include a timezone; `+08:00` and `Z` are accepted and normalized to UTC. Currency is SGD only. Unknown JSON fields are rejected.

## Person 1: bot

1. `POST /users` with `telegram_id` (string) and `display_name`. Store the returned `id`. Creation is idempotent by Telegram ID; a new user's `access_token` is returned once. For repeated users, the bot can keep using its service token. `POST /users/{id}/token` rotates an individual token if needed.
2. `POST /requests` with the example below. Store the returned request ID.
3. Display `GET /groups/{id}`. Each participant entry has `amount_minor`, `individual_total_minor`, `savings_minor`, `approved_version` and `payment_state`. Show the group version, pickup point, expected delivery and expiry.
4. On a verified participant callback, `POST /groups/{gid}/participants/{uid}/approve` with `{"version": 1}`.
5. In mock mode, `POST /groups/{gid}/participants/{uid}/authorize` with `{"version":1,"outcome":"success"}`. This is an explicitly simulated contribution. To test failure use `decline`; it can then be retried with `success`.
6. In Reap mode skip the contribution endpoint. Present the designated purchaser's hosted `approval_url` after execution. Let Person 4's worker reconcile; a redirect back alone is never proof of payment.
7. `GET /events?after=0` gives durable events and `next_cursor`. Poll and persist the cursor after successful Telegram delivery. Event `group_id` can be loaded to obtain recipients. `ORDER_PLACED`, `ORDER_FAILED`, `PROPOSAL_CREATED`, `REAP_QUOTE_READY`, `GROUP_EXPIRED` and refund events are useful notification triggers. Deduplicate events by ID.

```json
{
  "user_id": "<returned user ID>",
  "title": "Atomic Habits",
  "isbn": "9780735211292",
  "edition": "hardcover",
  "quantity": 1,
  "budget_minor": 3500,
  "min_savings_minor": 100,
  "currency": "SGD",
  "latest_delivery_at": "2026-10-20T18:00:00+08:00",
  "pickup_location": "NTU shared collection point"
}
```

Replace example dates with future dates. `POST /requests/{id}/cancel` cancels an unreserved request. To withdraw from a pending group use the participant `/withdraw` endpoint with the current version; the group is cancelled so the optimizer can rebuild it safely. Once checkout starts, withdrawal is blocked pending provider reconciliation.

## Person 2: merchant intelligence

Read `GET /requests?status=OPEN` and write `POST /offers`. Each offer belongs to one exact request:

```json
{
  "request_id": "<request ID>",
  "merchant": "kinokuniya.com.sg",
  "variant_id": "<Reap-resolved variant ID, or MOCK-1 in mock mode>",
  "unit_price_minor": 2500,
  "individual_total_minor": 3100,
  "currency": "SGD",
  "available": true,
  "delivery_at": "2026-10-15T18:00:00+08:00",
  "expires_at": "2026-10-09T20:00:00+08:00",
  "source": "mock"
}
```

`individual_total_minor` is the total for the request's entire quantity when ordered alone, including shipping/tax/fees. Return several offers for comparison. Accepted sources: `mock`, `merchant`, `reap`. In Reap mode, publish `source=reap` and use the purchasable variant returned by Reap discovery, not a product ID or a numeric Shopify variant copied from the spreadsheet. The backend refuses mock/merchant offers for the Reap quote endpoint.

Initial allowlist: `kinokuniya.com.sg`, `popular.com.sg`, `mossery.co`. Atomic Books is excluded because Singapore delivery is not supported. New offers are immutable records. Publishing a better feasible individual offer can make an older group fail its minimum-savings check at checkout.

## Person 3: optimizer

Read requests and offers; choose the merchant, members, pickup point, purchaser and allocations. Submit `POST /groups`:

```json
{
  "client_reference": "optimizer-proposal-unique-001",
  "merchant": "kinokuniya.com.sg",
  "purchaser_id": "<member user ID>",
  "total_minor": 5000,
  "currency": "SGD",
  "expires_at": "2026-10-09T20:00:00+08:00",
  "delivery_at": "2026-10-15T18:00:00+08:00",
  "pickup_location": "NTU shared collection point",
  "reason": "Recommend buying now because free delivery is reached.",
  "allocations": [
    {"request_id":"<Alice request>","offer_id":"<Alice offer>","amount_minor":2500},
    {"request_id":"<Bob request>","offer_id":"<Bob offer>","amount_minor":2500}
  ]
}
```

Two to twenty different requests are allowed. Multiple requests from one user are supported, and user approval covers all their requests in the group. Duplicate request IDs are rejected. A request cannot be reserved in two active groups. Repeated creation with the same `client_reference` and identical body returns the original group; changed content produces `IDEMPOTENCY_CONFLICT`.

Revise with `PUT /groups/{id}/proposal?expected_version=1`, keeping `client_reference`. A successful revision increments the version and clears every approval and mock authorization. Removed requests are released. You cannot revise a group after checkout begins. Failed, refunded and cancelled groups stay as history; create a new proposal reference to retry released requests.

For Reap add `shipping_address` using documented camelCase fields:

```json
{"firstName":"<recipient>","lastName":"<recipient>","phone":"<phone>",
 "addressLine1":"<shared address>","city":"Singapore","postalCode":"<six digits>","country":"SG"}
```

The backend stores the address for the provider but does not expose it in group views/events.

## Person 4: execute and monitor

- `POST /groups/{id}/execute`, `{"version":1}`: requires all current commitments. Mock mode also requires all simulated holds. Reap mode requires an active purchaser enrollment and a backend-created Reap quote. Duplicate execution returns the same order.
- `GET /orders/{id}`: status, provider, hosted approval URL, merchant reference and final charged amount.
- `POST /orders/{id}/reconcile`: retrieves provider status or replays the exact persisted checkout key within its retention window. Mock timeout reconciles to a deterministic success.
- `POST /groups/{id}/cancel`, `{"reason":"..."}`: only before checkout or after definite failure. Voids mock holds.
- `POST /orders/{id}/refund`, `{"reason":"..."}`: full mock refund, or Reap manual-review request.
- `GET /payments?group_id=...`: simulated AUTHORIZE / AUTHORIZATION_FAILED / VOID / CAPTURE / REFUND journal. In Reap mode the participants are not individually charged, so no contribution capture entries are created.
- `POST /maintenance/expire`: void stale mock holds and release requests.

Errors return `{"detail":{"code":"...","message":"..."}}`; schema validation uses FastAPI's standard 422 detail list. Handle `STALE_PROPOSAL` by reloading the group, `COMMITMENTS_MISSING` by waiting for buyers, and `PRICE_CHANGED`/`QUOTE_EXPIRED` by obtaining a new quote and fresh consent. Do not retry ambiguous payments as a new order.
