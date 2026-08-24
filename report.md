# One question, and what the data actually said

**The question the brief poses:** *how does the cash price compare to the total
cost under an installment plan?* I set out to answer exactly that — and the
answer turned out to be a finding about the **data itself**, not the prices.

The dataset: **521 listings** (306 sale / 215 rent) across Cairo, Alexandria,
and Giza; **517** with a full Group B extraction; **0** duplicate `listing_id`s.
Quality and evaluation numbers are summarized at the end and detailed in the
[README](README.md).

**Executive summary.** Of the 126 listings that advertise installments, only
**28** disclose a plan complete enough to cost out — so the dataset answers the
cash-vs-installment question honestly only for the minority that disclose. And
where a plan *is* stated, its total sums to **the list price**, not above it: the
financing premium a researcher expects to find hidden in the installments isn't
there. The one real methodological win below is the outlier audit — a first cut
said "20% of plans cost more than cash," and reading the source text turned that
into "~1, and one of those was a data bug."

---

## Finding 1 — the payment plan is advertised far more than it is *disclosed*

The payment plan is one of the most economically important terms in an off-plan
listing, and a core reason Group B exists. So: of the listings that
**advertise** installments, how many actually **state enough numbers** to know
what the plan costs?

| | count |
|---|---|
| Listings whose `payment_type` is `installments` or `both` | **126** |
| …that state **any** plan number (a down payment or an installment) | 81 (64%) |
| …that state a **complete, computable** plan (down + amount + frequency + years) | **28 (22%)** |

**Only ~1 in 5 listings that offer installments tell you what the installments
are.** The rest advertise financing as a headline and leave the actual terms —
the number an economist needs — for a phone call. This is the thing that would
bite a researcher silently: filter to "installments available" and you have 126
rows; try to compute a real cost and 98 of them evaporate. The dataset can
answer the brief's cash-vs-installment question — but honestly, only for the
minority of listings that disclose, and **the pipeline's job is to make that gap
visible rather than paper over it with a guessed plan.** (This is also why
`total_installment_cost` is null for the other 78%: the plan is genuinely
incomplete, and a null is the correct answer.)

---

## Finding 2 — for disclosed plans, the schedule sums to the list price

Taking every listing for which a `total_installment_cost` is computable at all
(**n = 35**), I compared it to the cash `price` (ratio = plan ÷ cash).

> **Why 35 here vs 28 in Finding 1:** Finding 1's 28 both *advertise* installments
> (`payment_type`) **and** state a complete plan. These 35 are all listings where
> a total is computable at all — the 28, plus 7 whose full plan numbers come from
> Bayut's structured data while the LLM left `payment_type` null (computable, just
> not "installment-typed"). Restricting to the 28 gives the same shape (median
> 0.92, the same two premium outliers, the same audit conclusion), so nothing here
> hinges on the choice.

> **Formula:** `total_installment_cost = down_payment + installment_amount ×
> payments_per_year × years`, where `payments_per_year` is 12 / 4 / 1 for
> monthly / quarterly / annual. (Frequencies in the data are monthly, quarterly,
> and annual; the computation uses the correct multiplier for each — verified,
> not assumed.)

| statistic | ratio (plan ÷ cash) |
|---|---|
| median | 0.985 |
| mean | 0.826 |
| min – max | 0.197 – 1.243 |
| **at exactly 1.000** (plan = list price) | **15 / 35** |
| below 0.90 (teaser — doesn't cover the balance) | 15 / 35 |
| above 1.02 (candidate premiums) | 2 / 35 |

**n = 35 is small — every conclusion below is stated as a listing count, not a
bare percentage, and I hand-audited the outliers before trusting them.**

**Reading the ratio:** a value *below* 1.0 does **not** mean the buyer pays less
than cash. It means the advertised `monthly × years` doesn't cover the whole
balance — a teaser schedule, not the full obligation (the mirror image of
Finding 1). So ratio 0.20 reads as "the schedule shown accounts for ~20% of the
price," never "buy it for 20%." That's why the mean (0.83) sits well below the
median (0.985): the teaser tail drags it down, it is not evidence of cheap plans.

The headline: the single largest cluster — **15 of 35 sits at exactly ratio
1.000** — the down payment plus the full installment schedule summing to the
listed cash price to the pound. This is consistent with the "0% interest"
language these listings use, in the specific sense that **the advertised schedule
equals the sticker price**; it is evidence of that, not proof the financing
carries no interest in a strict economic sense.

I then manually re-checked all **7** listings with ratio **above 1.0** (the 2
above 1.02, plus 5 sitting a rounding-hair over 1.000) against their
`description_raw`, because a "financing costs more than cash" claim is exactly
the kind of thing that should not ship unverified. What the audit found:

- **5 of the 7 exceed 1.0 by rounding only** (12–584 EGP on multi-million-pound
  prices, ratio 1.0000–1.0001). These are the "plan = price" case, **not**
  premiums — a naive `ratio > 1.0` filter miscounts them.
- **1 is a genuine, modest premium** (503963925, ratio 1.24 — 10% down + 40k/mo
  × 8 yr; the text supports it).
- **1 is a data artifact, not a premium** (503957958, ratio 1.10): Bayut's own
  structured `monthly_installment` there already amortizes the *full* price
  (price ÷ 144 months), yet a 10% down payment is *also* listed, so the down
  payment is double-counted. The listing's own text states the total **is** the
  list price.

So the honest count of plans that genuinely cost more than cash is **~1 of 35**,
not 7. The financing premium a researcher might expect to find hidden in the
installments **isn't there** — the plans sum to the sticker price. The real
premium lives in the opposite field: the **cash discount** advertised for paying
up front.

The methodological point is the whole point of the exercise: **the first cut
said "20% of plans cost more than cash"; auditing the 7 rows turned that into
"~1, and one of those was a double-counting bug."** The median is not the
finding — the audit is.

---

## Finding 3 — the brief's example question, answered; and a trap that didn't spring

Answering the brief's literal example on this slice — 3-bedroom sale apartments,
`city = 'New Cairo'`:

| | EGP / m² |
|---|---|
| **median** | **≈ 54,400** |
| range | ~11,000 – 161,000 |
| n | 31 |

That spread is real and worth a caveat: "New Cairo" bundles 1st-through-5th
Settlement, resale and off-plan, bare-shell and super-lux under one label, so
the median is a location anchor, not a like-for-like price. I deliberately do
**not** split it by `sale_type` or finishing here — at n = 31 each cut falls to
~10–15 rows, small enough that one luxury outlier swings the sub-median more than
the segmentation reveals. The honest move at this sample size is one median with
its spread stated, not four medians dressed up as precision.

And the trap the brief flags — *"some listings hide area"* — **did not fire
here: 0 / 521 listings had a null area.** Because Group A comes from Bayut's
Algolia index rather than scraped HTML, `area` was always present, so
`price_per_sqm` is computable for every sale listing. The pipeline still handles
null area correctly (it returns null, never a fabricated 0) — the data just
never exercised it. Worth stating plainly, because "we handle X" and "X occurred
in the data" are different claims, and only the second is evidence.

---

## Pipeline & data quality

| metric | value |
|---|---|
| Listings in dataset | 521 |
| Sale / rent | 306 / 215 |
| Governorates | Cairo 235 · Alexandria 144 · Giza 142 |
| Group B extracted | 517 |
| Listings with no description (→ Group B null) | 4 |
| Duplicate `listing_id`s | **0** |
| Fetch failures (bot wall) | 13 blocked + 22 exception — all retriable, none lost |
| Re-run safe / resumable | yes (idempotent upserts, per-stage pending queues) |
| Extraction cost | **$0.00** (Gemini free tier) |

**Evaluation** (25 hand-labeled listings; full per-field table in the
[README](README.md)): **hallucination rate ~0%** across every field, and the
high-value structured fields — location, compound, finishing, `sale_type`, and
the whole payment plan — score **90–100%**. The weak fields are tiny-sample or
free-text recall (`delivery_status` 67%, `floor_number` 77%), reported as-is
rather than rounded up.

---

### Method note (honesty)

All figures are `SELECT`s over `data/bayut.db`, reproducible. Ratios use the 35
listings with a *computable* `total_installment_cost` (see Finding 2 for how that
differs from Finding 1's 28); every count is stated with its denominator, a
plan missing any input yields null rather than a guess (which is why Finding 1's
78% is a feature of the extraction, not a gap in it), and the `payments_per_year`
multiplier is taken from each listing's actual `installment_frequency`, not
assumed monthly. Finding 2's outliers were **hand-audited against source text**
before reporting — 5 of 7 were rounding, 1 genuine, 1 a down-payment
double-counting artifact in Bayut's own structured data — which is what turned a
tempting "20% cost more than cash" into the honest "~1 of 35." That audit, not
the median, is the finding.
