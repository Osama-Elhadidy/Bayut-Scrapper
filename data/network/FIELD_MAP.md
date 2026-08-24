# Where every field actually comes from

Established by live network trace (`probes/network_trace.py`) against `القاهرة`
for-sale search + one detail page, plus direct probes of the Algolia index
(`probes/algolia_fetch.py`). Not inferred — every row below was read out of a real
captured response. Sample listing: `503976560`.

## The three sources, ranked

| # | Source | Cost | Bot risk | What it gives |
|---|--------|------|----------|---------------|
| 1 | **Algolia** `LL8IZ711CS` | 1 request / 24–100 listings | none observed | all of Group A **except** `description_raw` |
| 2 | **`__STRAT_SERVER_STATE__`** in detail-page HTML | 1 full page render / listing | Humbucker challenge | everything Algolia has **+ `description`** |
| 3 | the description free text itself | LLM call / listing | — | most of Group B |

There is **no fourth option**. Probed and ruled out:

- `www.bayut.eg/api/listing/{id}`, `/details`, `/description`, `/api/properties/{id}`,
  `/api/v1/listing/{id}`, and 6 more variants → **401, zero-length body**. The
  `/api/` namespace is real (the browser calls `/api/listing/{id}/permitNumber`,
  `/api/areaGuideLink`, `/api/generateShortLink`, `/api/v1/recommender/…`) but
  it is session-authenticated; nothing there returns a description anyway.
- `fenix-data-es6.bayut.eg/_msearch` → **401**. This is internal Elasticsearch.
  Out of scope by our own rule: stick to what the browser can legitimately call.
- In-app SPA click from search → listing produced **zero data-bearing requests**.
  The router does a full document navigation, so there is no cheap JSON route to
  piggyback on. `"DISABLE_SSR": false` — the page is server-rendered, and the
  description arrives inside that HTML or not at all.

## Group A

| Field | Source | Key |
|---|---|---|
| `listing_id` | Algolia | `externalID` |
| `url` | derived | `/تفاصيل-{id}/العقار.html` — see note below |
| `purpose` | Algolia | `purpose` (`for-sale` / `for-rent`) |
| `property_type` | Algolia | `category[-1].nameSingular` |
| `price` | Algolia | `price` |
| `price_period` | Algolia | `rentFrequency` |
| `currency` | constant | EGP |
| `bedrooms` | Algolia | `rooms` (+ STRAT `isStudio` — see note) |
| `bathrooms` | Algolia | `baths` |
| `area_sqm` | Algolia | `area` (`hidePrice` / null area is the spec's deliberate trap) |
| `location_raw` | Algolia | `location[]` joined |
| `agency_name` | Algolia | `agency.name` |
| `is_verified` | Algolia | `isVerified` |
| `date_listed` | Algolia | `createdAt` (epoch) |
| **`description_raw`** | **STRAT only** | **`property.data.mainDescription`** |
| `language` | derived | from the description |

## Group B

`S` = available structurally (no LLM needed). `T` = free text only.

| Field | | Source |
|---|---|---|
| `governorate` / `city` / `district` | S | Algolia `location[]` levels 1 / 2 / 3 — already normalized by Bayut |
| `delivery_status` | S | `completionStatus` (`completed` → ready, else off-plan) |
| `sale_type` | S | `extraFields.ownership` (`primary` / `resale`) |
| `compound_name` | S~ | `location[]` level 4 when present — **17%** of hits (`كومباوند هايد بارك القاهرة الجديدة`); free text otherwise |
| `down_payment_amount` | S~ | `downPayment` — populated on **28%** of hits; free text otherwise |
| `amenities` | S~ | `amenities` — present on 79% but thin (often just `سنة البناء`); free text is the real source |
| `finishing_level` | T~ | `furnishingStatus` only separates furnished/unfurnished. core&shell / semi / super lux are text-only |
| `developer_name` | T | free text (`isDeveloper` flag on the agency is a weak corroborator) |
| `delivery_date` | T | free text |
| `payment_type` | T | free text |
| `down_payment_pct` | T | free text |
| `installment_years` | T | free text |
| `installment_amount` | T | free text |
| `installment_frequency` | T | free text |
| `cash_discount_pct` | T | free text |
| `floor_number` | T | free text |
| `garden_area_sqm` / `roof_area_sqm` | T | free text (`plotArea` is the plot, not the garden) |
| `is_negotiable` | T | free text |

Derived: `price_per_sqm` (Group A only, computable without the LLM),
`total_installment_cost` (needs the text-only installment fields).

## Three corrections this trace forces

1. **Parse from `__STRAT_SERVER_STATE__`, not JSON-LD/DOM.** The detail HTML
   carries a 127KB server-rendered state blob:
   `<script id="browserInitialState">Object.defineProperty(window,
   "__STRAT_SERVER_STATE__", {"value":{…}})`. Inside it,
   `property.data` is a **72-key superset of the Algolia hit** plus
   `description` / `mainDescription`. It also holds `isStudio`, which Algolia
   does *not* expose — that is the clean answer to the spec's "studio handled
   sensibly", rather than inferring studio from the category name.
   Parsing this is strictly better than the current JSON-LD-then-DOM-regex
   chain: one JSON object, no hashed class names, and the description comes
   back **verbatim with its `<br />` markup** — which is what
   "full original text, unedited" asks for. JSON-LD hands back a
   tag-stripped copy.

2. **The URL scheme is `/تفاصيل-{id}/العقار.html`.** That is what the site's
   own router and `generateShortLink` use. `discover.py` builds
   `/ar/property/details-{id}.html`.

3. **The site's search index is `bayut-eg-production-ads-city-level-score-ar`**,
   not the `bayut-eg-production-ads-ar` in `config.py`. Both work and carry the
   same fields; the former is just ranked by city-level score.

## Extraction cost, restated

Group A + 4 Group B fields land free from ~21 Algolia requests with no bot
exposure. The LLM only ever needs to see the description, and only ~13 fields
genuinely depend on it. Everything structural should be passed to the model as
context (or asserted over its output), never re-derived by it.
