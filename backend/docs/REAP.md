# Reap integration findings and setup

Verified against the public Reap docs and the Reap × 65labs participant guide on 9 October 2026. Reap sandbox product search and four-item quote were exercised with a local key. The adapter's hosted enrollment and checkout contract was verified with mocked responses; no card enrollment or checkout has been completed.

## Verified capabilities

The [Agentic overview](https://docs.reap.global/agentic-payments/overview) describes hosted card enrollment, merchant discovery, quotes and approval. The [quote schema](https://docs.reap.global/api-reference/agentic/create-quote) accepts 1–20 items, a shipping address and an optional `offerCode`; its response includes shipping options and a final amount. [One-time purchases](https://docs.reap.global/agentic-payments/one-time-purchases) documents quote → checkout → hosted approval → provider-confirmed order reference. BookPool requires fresh participant approval of the final Reap quote before executing.

An [external enrollment](https://docs.reap.global/api-reference/agentic/create-enrollment) uses `source=EXTERNAL`, `owner={type:CLIENT_REFERENCE,id:<BookPool user ID>,email:<email>}`, and REDIRECT presentation. Test card entry belongs on the resulting hosted page. ACTIVE enrollment status is checked server-side before checkout. A normal checkout returns a hosted approval action; BookPool does not send `X-Simulate-Checkout: COMPLETED`, so the purchaser reviews the charge in the Reap flow.

The [hackathon guide](https://reap-hackathon-microsite.vercel.app/#build) mandates sandbox-only checkout, with no actual merchant purchases or deliveries. It requires meaningful Agentic or Kwal integration. Local mock mode is useful during component development, but is not sufficient on its own for the final integrated hackathon submission.

## Unsupported assumptions

The documented Agentic checkout takes one enrollment. It does not establish customer-fund pooling, an escrow/vault, multiple participant charges contributing to one order, or an Agentic merchant-refund endpoint. BookPool therefore records four simulated contribution confirmations and uses one designated purchaser for the single Reap sandbox payment. No participant contribution is charged. Reap refund requests require manual review; no completed refund is fabricated.

Production payments are intentionally disabled in this event build. A listed merchant and Singapore shipping policy do not guarantee your integration's sandbox checkout works. Product variant availability, market configuration, enrollment and hosted approval need testing with your team key.

## Local four-person sandbox run

1. Keep your test key private in `.env` or hosting secrets. Set `PAYMENT_MODE=reap_sandbox`, `REAP_API_KEY=<your key>` and the official sandbox base URL. Never put a PAN, CVV or expiry in `.env` or a request to BookPool. `BOOKPOOL_PURCHASER_EMAIL`, `BOOKPOOL_SHIPPING_NAME`, and `BOOKPOOL_SHIPPING_PHONE` may use the sandbox placeholders in `.env.example`.
2. Use a fresh database, e.g. `DATABASE_URL=sqlite:///./data/reap-sandbox.db`, and restart. `PUBLIC_BASE_URL` must identify the backend to which the hosted approval returns.
3. Run the backend, bot, and `python -m bookpool.orchestrator.worker` from the repository root. Four Telegram accounts create requests at a common approved store. The worker resolves Reap product variants, creates a group, and requests the provider quote for `41 Students Walk, Singapore 639549`.
4. The designated purchaser sends `/sandboxcard` to the Telegram bot. Open the hosted Reap URL and enter the test card there. The backend stores only the enrollment ID. Keep the card details out of Telegram and `.env`.
5. The bot sends the versioned Reap quote to all participants. Each presses **Approve This Offer**, then **Confirm simulated contribution**. These are BookPool ledger entries only.
6. When all four confirm, the worker starts one Reap checkout with the purchaser's enrollment. The purchaser opens the hosted approval URL sent by the bot. The backend confirms the order only after Reap reports completion and a merchant order reference.
7. For direct API use, call `POST /v1/users/{id}/enrollments` with `{"email":"<purchaser email>","return_url":"<PUBLIC_BASE_URL>/payment-return"}` and an `Idempotency-Key` header. Keep that header stable if the request times out. Reap discovery uses `POST /agentic/products/search` and `POST /agentic/products/details`; the worker saves offers with `source=reap` and provider variant IDs. `POST /v1/groups/{id}/reap-quote` accepts `{"version":1,"email":"<purchaser email>"}` and optional `offer_code` or `shipping_option_id`.
8. Read the returned group. Reap totals are allocated in proportion to the optimizer's existing allocations using deterministic largest-remainder rounding; the sum exactly matches the quote. The quote may violate budgets/savings and be rejected. The new quote increments the version and clears old approvals.
9. Quotes expire quickly. Requote and collect fresh approvals if the quote expires or changes.
10. Poll `POST /v1/orders/{id}/reconcile` or run the backend worker. Show ORDERED only after Reap confirms COMPLETED, a merchant reference and a valid final amount.

For an enrollment already created for a BookPool user, trusted service code may use `PUT /v1/users/{id}/enrollment` with `{"enrollment_id":"..."}`. The backend checks its CLIENT_REFERENCE owner and ACTIVE status. An enrollment owned by another user is rejected.

Provider Amount examples use major currency units, such as `35.29` SGD. BookPool converts them to integer cents without floating-point arithmetic. On 9 October 2026, the isolated four-person BookPool worker created a sandbox Kinokuniya quote for Atomic Habits, The Psychology of Money, Educated, and The Alchemist: S$85.36 total with S$0 shipping. No card enrollment or completed Reap transaction has yet been tested.

## Failure recovery

Provider timeouts/5xx/conflicts after checkout intent creation become PAYMENT_UNKNOWN. Keep the one persisted order. Retry reconciliation with the same payload and provider idempotency key, never a new charge. If an ambiguous checkout without a provider ID is older than 23 hours, manual reconciliation is required because documented idempotency retention is 24 hours. If a completed charge exceeds the consented total, BookPool records the actual amount and flags RECONCILIATION_REQUIRED without silently charging/allocating the extra amount to participants.
