# Reap integration findings and setup

Verified against the public Reap docs and the Reap × 65labs participant guide on 9 October 2026. No sandbox credentials were supplied or used during implementation. The adapter's HTTP contract was verified with mocked responses, not a real merchant checkout.

## Verified capabilities

The [Agentic overview](https://docs.reap.global/agentic-payments/overview) describes hosted card enrollment, merchant discovery, quotes and approval. The [quote schema](https://docs.reap.global/api-reference/agentic/create-quote) accepts 1–20 items, a shipping address and an optional `offerCode`; its response includes shipping options and a final amount. [One-time purchases](https://docs.reap.global/agentic-payments/one-time-purchases) documents quote → checkout → hosted approval → provider-confirmed order reference. BookPool requires fresh participant approval of the final Reap quote before executing.

An [external enrollment](https://docs.reap.global/api-reference/agentic/create-enrollment) uses `source=EXTERNAL`, `owner={type:CLIENT_REFERENCE,id:<BookPool user ID>,email:<email>}`, and REDIRECT presentation. Test card entry belongs on the resulting hosted page. ACTIVE enrollment status is checked server-side before checkout. A normal checkout returns a hosted approval action; BookPool does not send `X-Simulate-Checkout: COMPLETED`, so the purchaser reviews the charge in the Reap flow.

The [hackathon guide](https://reap-hackathon-microsite.vercel.app/#build) mandates sandbox-only checkout, with no actual merchant purchases or deliveries. It requires meaningful Agentic or Kwal integration. Local mock mode is useful during component development, but is not sufficient on its own for the final integrated hackathon submission.

## Unsupported assumptions

The documented Agentic checkout takes one enrollment. It does not establish customer-fund pooling, an escrow/vault, multiple participant charges contributing to one order, or an Agentic merchant-refund endpoint. BookPool therefore uses one purchaser for Reap sandbox payment and records the other participants' agreement without charging them. Reap refund requests require manual review; no completed refund is fabricated.

Production payments are intentionally disabled in this event build. A listed merchant and Singapore shipping policy do not guarantee your integration's sandbox checkout works. Product variant availability, market configuration, enrollment and hosted approval need testing with your team key.

## Configure later

1. Keep your test key private in `.env` or hosting secrets. Set `PAYMENT_MODE=reap_sandbox`, `REAP_API_KEY=<your key>` and the official sandbox base URL. Never put a PAN, CVV or expiry in `.env` or a request to BookPool.
2. Use a fresh database, e.g. `DATABASE_URL=sqlite:///./data/reap-sandbox.db`, and restart. `PUBLIC_BASE_URL` must identify the backend to which the hosted approval returns.
3. Create the designated purchaser with `POST /v1/users`.
4. Call `POST /v1/users/{id}/enrollments` with `{"email":"<purchaser email>","return_url":"<PUBLIC_BASE_URL>/payment-return"}` and an `Idempotency-Key` header. Keep that header stable if the request times out. Open `nextAction.url` and personally enter your test card on the hosted Reap page. The backend stores only the enrollment ID.
5. Person 2 resolves supported products using Reap discovery, saves offers with `source=reap`, and supplies the returned purchasable variant IDs.
6. Person 3 submits a group with the shared Singapore shipping address. Call `POST /v1/groups/{id}/reap-quote` with `{"version":1,"email":"<purchaser email>"}`. `offer_code` is optional. `shipping_option_id` is optional; if omitted the provider's selected option is used. Requote with a known option ID to choose another shipping service.
7. Read the returned group. Reap totals are allocated in proportion to the optimizer's existing allocations using deterministic largest-remainder rounding; the sum exactly matches the quote. The quote may violate budgets/savings and be rejected. The new quote increments the version and clears old approvals.
8. Have every participant approve that version. Execute it. Present `approval_url` to the purchaser and have them approve on Reap's hosted page.
9. Poll `POST /v1/orders/{id}/reconcile` or run the worker. Show ORDERED only after Reap confirms COMPLETED, a merchant reference and a valid final amount.

For an enrollment already created for a BookPool user, trusted service code may use `PUT /v1/users/{id}/enrollment` with `{"enrollment_id":"..."}`. The backend checks its CLIENT_REFERENCE owner and ACTIVE status. An enrollment owned by another user is rejected.

Provider Amount examples use major currency units, such as `35.29` SGD. BookPool converts them to integer cents without floating-point arithmetic. Reap success/failure, quote expiry and idempotency semantics still require a smoke test against your configured environment before presenting it as a working Reap integration.

## Failure recovery

Provider timeouts/5xx/conflicts after checkout intent creation become PAYMENT_UNKNOWN. Keep the one persisted order. Retry reconciliation with the same payload and provider idempotency key, never a new charge. If an ambiguous checkout without a provider ID is older than 23 hours, manual reconciliation is required because documented idempotency retention is 24 hours. If a completed charge exceeds the consented total, BookPool records the actual amount and flags RECONCILIATION_REQUIRED without silently charging/allocating the extra amount to participants.
