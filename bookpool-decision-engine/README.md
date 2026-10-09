# BookPool — Decision Engine (Person 3)

Group optimisation and agentic buy-now decisions for BookPool. Pure Python, deterministic money logic, no LLM needed. Works today on **simulated** merchant data; swaps to real data with no code changes once Person 2's service returns the same shapes.

```
pip install -r requirements.txt
python demo.py            # Alice → Bob → Charlie scenario from the brief
python -m pytest          # 30 tests
uvicorn decision_engine.api:app --port 8003   # optional HTTP service
python export_schemas.py  # writes JSON Schemas to schemas/ for teammates
```

## What it does

Given all **open** requests and current offers, `DecisionEngine.evaluate(...)` returns:

| Output | Meaning |
|---|---|
| `proposals: list[GroupProposal]` | Groups to buy together: merchant, each person's cost vs buying alone, savings, delivery date, **recommendation (BUY_NOW / WAIT)** with reasons and `order_by` date |
| `solo_plans: list[SoloPlan]` | For everyone not grouped: `WAITING_FOR_GROUP`, `BUY_SOLO_RECOMMENDED`, `DEADLINE_UNREACHABLE` or `UNSERVABLE` |
| `events: list[EngineEvent]` | Proactive notifications (group formed, members joined, merged, split, free delivery unlocked, buy-now, price drop…) with ready-to-send text and a `dedupe_key` |

### Rules (all configurable in `EngineConfig`)

- **Grouping constraints:** same pickup point, one merchant, everyone's expected delivery ≤ their deadline, within budget, stock sufficient, max 8 per group, and **every member saves ≥ S$0.01 vs their cheapest on-time solo option** — nobody is ever made worse off.
- **Objective:** maximise total savings. Exact optimal partition (subset DP) up to 12 requests per pickup point; greedy merge beyond that (40 requests ≈ 1s).
- **Cost split:** each person pays their own book; volume discounts are shared pro-rata; the delivery fee (if any) is split **equally** (or `proportional` to book value). Cents always add up exactly.
- **Deadlines:** expected delivery = today + `order_processing_days` (1, for approvals/payment) + merchant delivery days. `order_by` = latest day the group can order and still meet every deadline.
- **Buy now vs wait:** waiting is worth it only if *expected* extra saving per person from more buyers ≥ S$0.50. Expected = (what more buyers could unlock: free delivery or next volume tier) × P(enough compatible buyers arrive before `order_by`), with arrivals modelled as Poisson (0.5/day by default). Overrides that force BUY_NOW: deadline within 1 day, low stock, price drop ≥ 5%, group full.
- **Solo:** recommend buying alone when delivery is already free, when the max possible group saving is below the user's `min_savings_to_wait`, or when the deadline is close.

## How teammates integrate

### Person 4 (backend) — calls the engine
```python
from decision_engine import DecisionEngine
engine = DecisionEngine()
result = engine.evaluate(open_requests, offers, merchants, now=now,
                         previous=last_result, previous_offers=last_offers)
```
- Call it whenever a request is added/cancelled, offers refresh, **and on a timer (e.g. hourly)** so deadline-driven recommendations fire.
- Pass only **open** (not yet committed) requests. Once a group is approved, lock those requests out of the pool.
- Store `result` and pass it back as `previous` next time — that's how the engine knows what changed and avoids repeat notifications. `group_id` is deterministic (merchant + members), so an unchanged group keeps its id.
- Send each `events[i]` once per `dedupe_key`. `needs_user_decision=True` means show approve/wait buttons.
- When everyone approves a `GroupProposal`, `participants[i].total` is the amount to collect from that user, and `total` is what goes to the merchant.
- HTTP alternative: `POST /v1/evaluate` with `EvaluateInput` JSON. Money is sent as strings (`"27.90"`).

### Person 2 (merchant intelligence) — supplies data
- `MerchantPolicy`: `shipping_fee`, `free_shipping_threshold`, optional `volume_discounts`, `delivers_to`, `approved`.
- `MerchantOffer`: one per (merchant, book): `price`, `in_stock`, `stock_qty` (or null if unknown), `delivery_days`.
- Match `book_id` to the request's `book_id` (ISBN-13 recommended). Set `is_simulated=true` on anything not verified.
- `decision_engine/mock_data.py` shows the exact shape; replace it with real data.

### Person 1 (Telegram bot) — displays results
- `participant_summary(proposal, request_id)` gives a per-user approval message.
- `event.message` is ready to send; you may rephrase it with the LLM, but **never let the LLM change the numbers**.
- `GroupProposal.reasoning` and `recommendation.explanation` give the explanation behind each decision.
- Requests need: `book_id`, `deadline` (date), `pickup_point`; optional `max_budget`, `min_savings_to_wait`, `preferred_merchants`, `quantity`.

## Layout

```
decision_engine/
  models.py      shared data contracts (pydantic)
  money.py       Decimal rounding + exact cent splitting
  costing.py     individual and group cost, deadlines, discounts, shipping
  optimizer.py   who buys with whom (exact DP + greedy)
  decisions.py   buy-now vs wait, solo outlook
  messages.py    deterministic user-facing text
  engine.py      DecisionEngine.evaluate + change/event detection
  api.py         optional FastAPI service
  mock_data.py   SIMULATED Kinokuniya / POPULAR data
demo.py, tests/, export_schemas.py
```

## Known limits / next steps
- Arrival rate is a fixed guess; estimate it from real request history once there is some.
- One pickup point per group; multiple pickup locations would need delivery-cost modelling.
- All mock prices, fees and thresholds are fictional; check real store terms before the pitch.
