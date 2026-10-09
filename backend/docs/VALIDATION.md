# Validation — 9 October 2026

## Verified

- Python 3.12: `python -m pytest -q` — **20 passed**. One dependency deprecation warning from Starlette/AnyIO.
- Local HTTP server started using `scripts/run_local.py --port 8765`.
- All five HTTP demo scenarios exited successfully: success → ORDERED; decline → FAILED; timeout → PAYMENT_UNKNOWN → ORDERED using the same order; refund → REFUNDED; withdrawal → CANCELLED with voided holds.
- Tests cover versioned approval, budget/savings/deadline/pickup checks, reservation conflicts, identity isolation, expiry, duplicate/concurrent execution, immutable simulated payment records and failure recovery.
- Reap adapter tested with mocked provider responses and HTTP transport: sandbox host/headers, hosted approval, final amount reconciliation, uncertain checkout replay, stale idempotency rejection, price change rejection, and redirect origin restriction.
- `docker compose config --quiet` passed with temporary local configuration secrets. This validates Compose configuration only.
- OpenAPI schema exported to `docs/openapi.json`.

## Pending environment checks

- Your Reap sandbox credentials remain blank. No real Reap API request, card enrollment, merchant checkout or provider refund was performed.
- Docker was stopped on this machine. The image, PostgreSQL transactions and worker in Compose have not been exercised at runtime. Local tests use SQLite.
- Cloud deployment and connection to Persons 1–3 are pending their services and hosting settings. The integration contract and deployment instructions are included.
- Sandbox merchant/variant availability and multi-item fulfillment must be verified with your account. Fixture prices are fictional; the backend relies on Person 2 for current merchant offers.

No actual funds, card data or merchant deliveries were involved in validation.
