# Proposed bot/backend API contract

**Status: historical proposal; these paths are not implemented by the current backend.** See [`../../backend/docs/INTEGRATION.md`](../../backend/docs/INTEGRATION.md) for the live service contract and [`../bot/bookpool_bot/team_backend.py`](../bot/bookpool_bot/team_backend.py) for the bot adapter. The remaining differences are documented in [`../bot/README.md`](../bot/README.md). The design below is retained for future API discussions.

## Transport

- JSON; ISO 8601 dates and timezone aware timestamps. Singapore is the initial business timezone.
- Monetary amounts are integer SGD cents (`*_minor`). No float money fields.
- `Authorization: Bearer <BACKEND_API_KEY>` for bot service requests. Production must use HTTPS and restrict the credential to bot actions.
- Responses may add fields; existing required fields should remain stable.
- `404` means not found or unauthorized, `409`/`410`/`412` means stale/expired/conflicting state, `429` means rate limit, and `5xx` is transient. The bot shows a generic retry message for service failures.

## Endpoints

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| `PUT` | `/v1/users/{telegram_user_id}` | `User` | `User` |
| `POST` | `/v1/book-requests` | `BookRequestInput` plus `telegram_user_id` | `BookRequest` |
| `PUT` | `/v1/users/{id}/book-requests/{request_id}` | `BookRequestInput` | `BookRequest` |
| `POST` | `/v1/users/{id}/book-requests/{request_id}/cancel` | empty | `BookRequest` |
| `GET` | `/v1/users/{id}/book-requests` | none | `BookRequest[]` |
| `GET` | `/v1/users/{id}/book-requests/{request_id}` | none | `BookRequest` |
| `GET` | `/v1/users/{id}/group-proposals` | none | `GroupProposal[]` |
| `GET` | `/v1/users/{id}/group-proposals/{proposal_id}` | none | `GroupProposal` |
| `POST` | `/v1/users/{id}/group-proposals/{proposal_id}/decisions` | `{"quote_version": 1, "decision": "ready"}` plus `Idempotency-Key` header | `DecisionResult` |
| `GET` | `/v1/bot/events?limit=20` | none | `NotificationEvent[]` |
| `POST` | `/v1/bot/events/{event_id}/ack` | empty | `204` |

`PUT /v1/users/{id}` uses merge upsert semantics: fields omitted from the JSON body retain their existing values. The bot sends only explicitly set fields, so `/start` cannot silently clear an earlier pickup confirmation or preference.

`decision` is one of `ready`, `wait`, `approve`, `decline`. The backend must validate ownership, current quote version, expiry, request state, price, and final approval eligibility. It must deduplicate by idempotency key. `ready` is willingness to proceed, while `approve` is final consent to one version. Neither endpoint should directly charge or place an order merely from a button press. If a quote changes, return `409` and issue a fresh proposal.

## Example request and response

`POST /v1/book-requests`

```json
{
  "telegram_user_id": 123456,
  "title": "Atomic Habits",
  "isbn": null,
  "author": null,
  "edition": null,
  "language": "English",
  "format": "paperback",
  "latest_delivery_date": "2026-10-19",
  "maximum_budget_minor": 3500,
  "minimum_savings_minor": 0,
  "minimum_savings_percent": null,
  "preference": "lower_cost",
  "currency": "SGD",
  "pickup_location_id": "ntu"
}
```

```json
{
  "request_id": "req_123",
  "telegram_user_id": 123456,
  "title": "Atomic Habits",
  "isbn": null,
  "author": null,
  "edition": null,
  "language": "English",
  "format": "paperback",
  "latest_delivery_date": "2026-10-19",
  "maximum_budget_minor": 3500,
  "minimum_savings_minor": 0,
  "minimum_savings_percent": null,
  "preference": "lower_cost",
  "currency": "SGD",
  "pickup_location_id": "ntu",
  "status": "searching",
  "created_at": "2026-10-09T12:00:00+08:00",
  "updated_at": "2026-10-09T12:00:00+08:00"
}
```

## Proposal and event shape

`GroupProposal` requires `proposal_id`, `request_id`, `telegram_user_id`, `merchant_name`, `participant_count`, `individual_total_minor`, `item_price_minor`, `shipping_minor`, `other_fees_minor`, `group_total_minor`, `savings_minor`, `currency`, `free_shipping_unlocked`, `estimated_delivery_date`, `quote_version`, `expires_at`, `status`, `recommendation_reason`, `payment_status`, and `is_estimate`. The backend should supply a user specific total and aggregate participant count only; never expose other buyers' personal data. The sum of components should equal `group_total_minor`. Merchant IDs and detailed stock information can be added once Person 2's contract is agreed.

`NotificationEvent` requires `event_id`, `event_type`, `telegram_user_id`, optional `request_id` and `proposal_id`, `occurred_at`, and optional `payload`. The bot recognizes `group_found`, `quote_changed`, `buy_now_recommended`, `final_approval_requested`, and `order_placed`; other types receive a generic status prompt. Event IDs must be stable across retries. Deliver at least once and retain until acknowledged. The event consumer should lease events in a multi worker deployment to prevent concurrent deliveries.

The backend must only emit `order_placed` after authoritative completion. It should emit a new proposal and `quote_changed` after repricing. The bot does not infer recommendation reasons or recalculate quotes.
