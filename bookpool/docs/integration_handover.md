# Integration handover

The bot now has a `BACKEND_MODE=team` adapter for the backend in this repository. Its verified flow is: Telegram user registration, request creation/list/cancel, group quote display, versioned approval/withdrawal, and durable event delivery. The cross-component test is [`../integration_tests/test_team_backend.py`](../integration_tests/test_team_backend.py). The implemented backend contract is [`../../backend/docs/INTEGRATION.md`](../../backend/docs/INTEGRATION.md).

**Person 2: Merchant intelligence** should publish live offers with an individual delivered total, stock, delivery estimate, and quote expiry. The backend allowlist currently includes `kinokuniya.com.sg`, `popular.com.sg`, and `mossery.co`. The bot does not scrape merchants. The backend group view has only the final per-user allocation, so item/shipping amounts and a free-shipping flag remain unavailable to the bot.

**Person 3: Optimisation and decision engine** should turn compatible offers into group proposals and revise the version if the quote changes. The backend enforces user budget, savings, and deadline constraints. The bot presents the resulting quote and reason; it does not calculate them. The decision engine and backend pass their own tests, but no automated job connecting the two is present here.

**Person 4: Backend and payments** owns contribution authorization, execution, payment reconciliation, and order events. Telegram approval records consent to one quote version. It does not authorize a contribution or execute an order. The bot does not collect payment credentials. A Reap purchaser approval URL is not yet surfaced in the bot.

Current contract gaps affecting the bot:

- Backend requests require a title and absolute budget, support savings in cents, and cannot be edited after creation. The bot checks these limits in team mode.
- Backend proposals support final approve/withdraw, but not readiness or waiting. Withdrawal cancels the group for all participants. The bot labels that effect in the button.
- Author, language, and reading preferences do not have backend request fields. Bot preferences are local to `TEAM_BACKEND_STATE_FILE`.
- Backend events identify a group, not Telegram recipients. The bot uses its persistent Telegram-to-backend user map to deliver them. Missing mappings block that event; recipient IDs or a user lookup endpoint would remove the dependency. Run one bot worker per state file.
- The service token can list global requests and groups. The bot filters them by the current backend user ID. Dedicated user-scoped list endpoints would be more efficient as data grows.

Use `BACKEND_MODE=team`, `BACKEND_BASE_URL`, and `BACKEND_API_KEY` to run with the current backend. `BACKEND_MODE=http` targets the older proposed API in [`bot_api_contract.md`](bot_api_contract.md) and is not compatible with the current backend.
