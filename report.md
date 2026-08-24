# One question, and what the data actually said

**The question the brief poses:** *how does the cash price compare to the total
cost under an installment plan?* I set out to answer exactly that — and the
answer turned out to be a finding about the **data itself**, not the prices.

The dataset: **521 listings** (306 sale / 215 rent) across Cairo, Alexandria,
and Giza; **517** with a full Group B extraction.

---

## Finding 1 — the payment plan is advertised far more than it is *disclosed*

The payment plan is the single most economically important term in an off-plan
listing, and the whole reason Group B exists. So: of the listings that
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

## Finding 2 — where a plan *is* stated, the "average" is a lie; the spread is the story

For the **35** listings with a fully computable plan, I compared the computed
`total_installment_cost` to the cash `price` (ratio = plan ÷ cash):

| statistic | ratio (plan ÷ cash) |
|---|---|
| median | **0.98** |
| mean | 0.83 |
| min – max | 0.20 – 1.24 |
| plans costing **more** than cash | **7 / 35 (20%)** |

A naive aggregation would report *"median ≈ 0.98, so installments cost about the
same as cash"* — and that would be **meaningless**, exactly the trap the brief
warns against. The median hides two opposite realities:

- **20% of plans cost *more* than paying cash** (up to 1.24×) — a real, if quiet,
  financing premium the "0% interest" marketing language obscures.
- Many plans land *well below* 1.0 (down to 0.20) not because they're a bargain,
  but because the advertised `monthly × years` **doesn't cover the balance** —
  the schedule shown is a teaser, and the true obligation is larger than the
  stated plan. The arithmetic is what exposes it.

The economic takeaway isn't a single ratio — it's that **you cannot trust the
advertised plan to equal the price, in either direction**, and only a
field-level extraction that computes the total per listing reveals which is
which.

---

## Finding 3 — the brief's example question, answered; and a trap that didn't spring

Answering the brief's literal example on this slice:

> **Median price per m² for a 3-bedroom sale apartment in New Cairo: ≈ 54,400
> EGP/m²** (`city = 'New Cairo'`, n = 31; wide spread, ~11k–161k, reflecting
> everything from older resale to premium off-plan compounds under one label).

And the trap the brief flags — *"some listings hide area"* — **did not fire
here: 0 / 521 listings had a null area.** Because Group A comes from Bayut's
Algolia index rather than scraped HTML, `area` was always present, so
`price_per_sqm` is computable for every sale listing. The pipeline still handles
null area correctly (it returns null, never a fabricated 0) — the data just
never exercised it. Worth stating plainly, because "we handle X" and "X occurred
in the data" are different claims, and only the second is evidence.

---

### Method note (honesty)

All figures are `SELECT`s over `data/bayut.db`, reproducible. Ratios use only
listings with a *complete* plan, so the sample is small (n = 35) and every
percentage is stated with its denominator. `total_installment_cost` is
`down_payment + installment_amount × payments_per_year × years`; a plan missing
any input yields null, never a guess — which is why Finding 1's 78% is a feature
of the extraction, not a gap in it.
