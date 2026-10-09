# BookPool merchant intelligence

Person 2's service for the Reap × 65labs buildathon. It resolves a requested
book, restricts results to the event's approved **Books & Stationery**
merchants, and returns comparable individual offers. When a shipping address is
provided, totals come from a short-lived **Reap sandbox quote**—not from a
scraped storefront or an estimated checkout.

## What it does

- Searches Reap's catalog with `merchantPreference.mode = ONLY` for every
  official book merchant:
  `atomicbooks.com`, `baronfig.com`, `byndartisan.com`,
  `kinokuniya.com.sg`, `komorebistationery.com`, `mossery.co`,
  `popular.com.sg`, and `stationerypal.com`.
- Matches ISBN first; title/author/format matching is conservative and will not
  silently swap editions.
- Fetches product details and resolves a purchasable Reap variant.
- Creates one sandbox quote per candidate only when the caller supplies an
  email and delivery address. The quote supplies subtotal, shipping, tax, final
  total, shipping choices, and expiry.
- Exposes a same-merchant cart-quote endpoint for Person 3's group optimiser.

It never receives card details, opens checkout, or charges a user. Reap owns
the catalog lookup, merchant pricing, sandbox checkout, and hosted approval
flow.

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Add the team sandbox key to .env locally, then:
uvicorn main:app --reload --env-file .env
```

The service is then available at `http://127.0.0.1:8000`; FastAPI documents the
contract at `/docs`.

`REAP_API_KEY` is intentionally not committed. Copy the variable names from
[.env.example](.env.example), but load the actual sandbox key in the shell or a
local ignored `.env` file.

For front-end work before the key arrives, opt into visibly simulated fixture
data instead of accidentally treating it as a Reap result:

```bash
export BOOKPOOL_DEMO_MODE=true
uvicorn main:app --reload
```

## API contract

### `POST /merchant/offers`

Without a delivery context, this returns Reap catalog offers and their variant
IDs. With `buyer_email` plus `shipping_address`, it creates sandbox quotes and
adds exact merchant-priced `total_individual` values.

```json
{
  "title": "Atomic Habits",
  "author": "James Clear",
  "isbn": "9781847941831",
  "format": "paperback",
  "quantity": 1,
  "buyer_email": "avery@example.com",
  "shipping_address": {
    "firstName": "Avery",
    "lastName": "Tan",
    "phone": "+6581234567",
    "addressLine1": "1 Example Road",
    "city": "Singapore",
    "postalCode": "123456",
    "country": "SG"
  }
}
```

Each offer includes `reap_product_id`, `reap_variant_id`, availability, the
discovered book identity, match confidence, quote status, and quote expiry.
Sort by `total_individual_sgd` only when `quote_status` is `quoted`; otherwise
the result is a discovery price, not a landed cost.

### `POST /merchant/cart-quote`

Person 3 should pass selected **`offerId` values returned by `/merchant/offers`**
from one merchant to get the actual shared shipping total. The service resolves
these from a short-lived server-side cache, so callers cannot slip an arbitrary
or unapproved Reap variant into a cart. This endpoint creates a sandbox quote
only; it does not checkout or charge anything.

```json
{
  "items": [
    {"offerId": "reap:product-id:variant-id-from-offer-1", "quantity": 1},
    {"offerId": "reap:product-id:variant-id-from-offer-2", "quantity": 1}
  ],
  "buyer_email": "avery@example.com",
  "shipping_address": {
    "firstName": "Avery",
    "lastName": "Tan",
    "phone": "+6581234567",
    "addressLine1": "1 Example Road",
    "city": "Singapore",
    "country": "SG"
  }
}
```

The response leaves shipping allocation deliberately unassigned. The group
optimiser can fairly distribute the one cart-level shipping charge after it
checks deadlines and commitments.

## Reap limitations surfaced honestly

Reap's product discovery API does not expose structured ISBN, author, stock
count, merchant product URLs, delivery days, or a free-shipping threshold. The
service marks an ISBN as `confirmed` when it is visible in product data, or
`reap_exact_search_match` when Reap returned a title-compatible result for the
exact ISBN query. A visible conflicting ISBN stays discovery-only. A quote
reports the current shipping cost and whether it is zero for that basket, but
the threshold is returned as `null` with `not_exposed_by_reap` rather than
fabricated.

## Verify

```bash
python3 -m unittest discover -s tests -v
```

The tests cover ISBN-first matching, edition safety, landed-cost ordering,
shipping-threshold fixture behaviour, Reap's restrictive merchant preference,
and sandbox-quote normalisation.
