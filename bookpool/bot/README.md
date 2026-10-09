# BookPool Telegram bot

BookPool's Telegram interface is independently runnable in **fictional demo mode**. It uses aiogram 3, a typed backend client, a deterministic request parser with optional Ollama, and an event poller. Merchant data, grouping decisions, payment, and order state belong to other services. The mock backend supplies fictional values only.

## Architecture

`handlers/` separates commands, request conversation, group actions, and fallback messages into aiogram routers; `common.py` holds shared flow helpers. `parser.py` extracts only user supplied fields and falls back to deterministic parsing if an LLM is absent. `models.py` defines validated data crossing the bot/backend boundary. `backend.py` contains the fictional mock and an earlier proposed HTTP client; `team_backend.py` adapts the current backend in this repository. `merchant_search.py` checks public product search results from the two approved bookstores in team mode. `presentation.py` formats backend values, and `notifications.py` polls and delivers backend events. The FSM keeps drafts; the backend owns submitted requests, proposals, and decisions.

The implemented service contract is in [`../../backend/docs/INTEGRATION.md`](../../backend/docs/INTEGRATION.md). [`../docs/bot_api_contract.md`](../docs/bot_api_contract.md) records the earlier proposal and is not implemented by the current backend.

## Local setup

1. In Telegram, message **@BotFather**, run `/newbot`, choose a name and username, and copy the token. Set the bot commands `/start`, `/help`, `/new`, `/myrequests`, `/groups`, `/settings`, `/cancel`, `/resume` in BotFather if desired.
2. Install Python 3.12+ and `uv`.
3. In `bookpool/bot`, run:

   ```sh
   cp .env.example .env
   # Edit .env and set TELEGRAM_BOT_TOKEN to your BotFather token.
   UV_CACHE_DIR=/private/tmp/bookpool-uv-cache uv sync --python python3.12 --extra dev
   .venv/bin/bookpool-bot
   ```

4. Open a **private** chat with the bot and send `/start`. Confirm NTU pickup. The default `BACKEND_MODE=mock` needs no backend service or LLM key.

If your shell already has writable uv cache access, `uv sync --extra dev` is sufficient. Never commit `.env`.

## Tests and formatting

```sh
cd bookpool/bot
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format .
```

To test the bot adapter against the actual backend app, install both packages into a test environment and run `python -m pytest bookpool/integration_tests -q` from the repository root. This test uses a temporary SQLite database and does not need a Telegram token.

## Fictional end to end demo

1. Send `/start`, then confirm **Yes, NTU pickup**.
2. Send `I want Atomic Habits, paperback, under S$35, and I can wait 10 days.` Choose **Any savings** and **Start Searching**. Use `/myrequests` to see the saved request.
3. Send `/demo group`. A fictional group quote arrives within `EVENT_POLL_SECONDS`. The prices are not merchant results.
4. Send `/demo recommend`. Read the backend supplied recommendation and press **I'm Ready to Buy** or **Keep Waiting**. Readiness is recorded without payment.
5. Send `/demo approval`. Review the price breakdown, then press **Approve This Offer**. Approval records consent to that quote version only.
6. To demonstrate a changed quote, send `/demo reprice`, then press a button from the old approval message. The old button is rejected; review the new quote.
7. Send `/demo complete` to simulate a backend order completion event. The message explicitly says demo mode.

The demo commands work only with `BACKEND_MODE=mock`. Mock state, pending events, and unfinished drafts reset on restart. An actual Telegram token is required to see the chat experience; automated tests do not require one.

## Configuration

See `.env.example`. For the backend in this repository, set `BACKEND_MODE=team`, `BACKEND_BASE_URL=http://localhost:8000`, and `BACKEND_API_KEY` to its service key. Start the backend using [`../../backend/README.md`](../../backend/README.md), then start the bot. On Start Searching, the bot saves the request and checks live public listings at Kinokuniya and POPULAR. The user does not need to choose a listing. Use **Check store prices** under `/myrequests` to refresh the listings. This checks displayed book prices and availability only; delivery cost and NTU delivery date are not verified, and no backend group offer is created from a listing. Automatic group matching is not active yet. `TEAM_BACKEND_STATE_FILE` retains Telegram-to-backend user IDs, bot-only preferences, and the delivered event cursor; keep it on persistent storage for a deployment. `BACKEND_MODE=http` targets the earlier proposed API and does not work with this backend. `LLM_PROVIDER=ollama` plus `LLM_MODEL` enables local structured extraction. `LLM_PROVIDER=openai_compatible` uses `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY` for an optional JSON-capable external Chat Completions endpoint. The parser falls back automatically when either provider is unavailable. The current FSM storage option is `memory`. Set `EVENT_POLL_SECONDS` for backend event polling. `DEFAULT_TIMEZONE` is reserved for future multi campus operation; MVP deadlines use Asia/Singapore.

## Known MVP limitations

- Mock data is process local; restarting clears requests and proposals. Use `BACKEND_MODE=team` for backend persistence.
- The deterministic parser supports common English dates and relative days. Ambiguous phrases such as “next Friday” are collected through a follow up rather than guessed.
- The current backend requires a title and an absolute SGD budget, and accepts minimum savings only in cents. The bot asks for a budget and rejects unsupported percentage savings in team mode. Submitted request editing is unavailable; cancel and create a new request instead.
- The current backend has approval and withdrawal, but no ready/wait decision. Withdrawal cancels the proposal for everyone. It supplies no per-user item/shipping breakdown or free-shipping flag, so the bot does not invent one. Author, language, and reading preferences are not stored in backend requests.
- `BACKEND_MODE=team` persists a delivered event cursor locally after every recipient receives an event. It derives recipients from known Telegram-to-backend user mappings. If that mapping is missing, the event remains pending; the backend should eventually supply recipients or a user lookup endpoint. Use one bot worker with this local state file.
- Live catalog discovery currently checks the public Shopify search endpoints of Kinokuniya and POPULAR. Results can change or fail if a retailer changes its storefront. A listing is not a delivered quote. Merchant shipping/checkout validation, optimizer group creation, contribution authorization, and order execution still need their owning services. Approving a group in Telegram records consent; it does not itself place an order. A live Telegram chat and payment checkout have not been verified by the automated tests.
