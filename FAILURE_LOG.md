# Failure log — the real problems, and how I got past them

The brief asks for the *actual* obstacles to getting the data out — bot walls,
captchas, dynamic JS injection, rate limits — not coding bugs. This is that log.
Every problem below is one I hit live against `bayut.eg`, with the concrete
signal that revealed it and the fix that beat it. The counts at the end come
straight from `python cli.py failures` on the real run.

---

## 1. The bot wall: Humbucker + reCAPTCHA Enterprise on *every* HTML page

**Problem.** Bayut fronts every HTML page — home, search, and detail — with an
in-house bot wall called **Humbucker**, with **reCAPTCHA Enterprise** underneath
(both are named in the site's own client-side JS config). A cold request doesn't
get a `403` or a Cloudflare interstitial; it gets a `Www-Authenticate:
hb-challenge, hb-captcha` header and a redirect into `/captchaChallenge`
(hCaptcha). The Algolia search API — a *different host* — is the only thing not
behind it, which is why discovery is free and fetch is the entire fight.

**How I got past it.** Not with a bypass — with a real browser that looks like a
returning human. The working recipe, arrived at by elimination (see §2):

- **Real installed Chrome**, `channel="chrome"` in Playwright — not the bundled
  Chromium (§3).
- **A homepage warm-up** before any deep link, so Humbucker sets its trust
  cookies on first contact instead of seeing a cold jump straight to a detail
  URL.
- **`playwright-stealth`** to sand off the obvious automation fingerprints.
- **Aggressive route-blocking**: of the ~300 sub-requests a listing page fires,
  allow only the document itself and Humbucker's own challenge scripts, abort
  everything else. Fewer requests is also a smaller challenge surface.

---

## 2. Cookies alone don't clear it — the challenge needs live JS every time

**Problem.** The intuitive shortcut — grab a valid session's cookies, then fetch
cheaply with `httpx`/`curl_cffi` — **fails**. I tried it with a genuinely valid
prior session's cookies and still got challenged. The wall isn't checking for a
cookie; it's checking that a real JS engine executes its challenge script on
each navigation.

**How I got past it.** Stopped trying to avoid the browser. The escalation, each
row killing one hypothesis:

| Attempt | Result |
|---|---|
| `httpx` + browser User-Agent | challenged |
| `curl_cffi` impersonating Chrome's TLS fingerprint, no cookies | challenged |
| `curl_cffi` **+ a valid session's cookies** | **still challenged** — proves it's JS execution, not cookies |
| Playwright + bundled Chromium + stealth | reload loop (§3) |
| Playwright + **real Chrome** + stealth + warm-up | ✅ clean fetch, first try |

The conclusion the table forces: there is no cookie/header/TLS trick that
substitutes for actually running the challenge JS. So the design commits to a
real browser and spends its effort on *pacing* instead of *bypassing*.

---

## 3. Bundled Chromium is itself the fingerprint — a client-side reload loop

**Problem.** Playwright's *bundled* Chromium, even with stealth patches, doesn't
just get challenged — it falls into a **client-side reload loop**: the page
keeps navigating, and `page.content()` throws mid-navigation because the DOM is
being torn down as you read it. The bundled binary carries automation tells the
real Chrome build doesn't.

**How I got past it.** Two changes:

1. **Use real Chrome** (`channel="chrome"`), which clears the challenge where
   bundled Chromium loops.
2. **Read the document body off the wire, not off the live DOM.** I capture the
   response text from Playwright's `response` event the moment it arrives — it's
   immutable — instead of calling `page.content()`, which re-serializes a live
   DOM that may be reloading. This alone removes the "threw mid-navigation"
   failure class entirely.

---

## 4. It's velocity/IP-reputation scored — a burst degrades even the homepage

**Problem.** A real browser buys a *chance* at a clean fetch, not a guarantee.
After ~15–20 automated navigations from one IP inside an hour, **every**
subsequent navigation started getting challenged — home and search pages
included, not just detail pages. This isn't a per-request or per-session defeat;
it's a **score that accumulates against the IP** and tips a threshold.

**How I got past it (and how I confirmed the diagnosis).**

- **Confirmed the cause** rather than guessing: [`probes/ip_check.py`](probes/ip_check.py)
  logs the public IP and whether Bayut challenges from it, before and after a
  router reconnect — separating IP-scoring from fingerprint/cookie scoring. A
  fresh IP with the same machine behaved differently, which points at IP
  reputation, not the browser fingerprint.
- **The lever is pacing, not cleverness**: long human-scale delays with jitter
  between listings, small batches, and one continuous browser session for the
  whole run (a cold process landing on a deep URL is itself a signal).
- **A circuit breaker** stops the whole run the moment even a warm-up navigation
  is challenged — because once the score is tripped, continuing only drives it
  further down and burns the queue into dead-letter rows. Stop, let the score
  decay, resume later (the pipeline is built for exactly this).

---

## 5. The 200-OK trap: a challenge page *looks* like success

**Problem.** The single most dangerous failure mode, because it's silent. The
challenge response is **HTTP 200** with a **~750–830 KB** body of obfuscated JS.
So `response.ok` is `True`, and even a "is the body big enough?" check passes —
on a page containing **zero listing data**. A naive scraper would happily store
800 KB of captcha JS as a "successful" listing and never notice.

**How I got past it.** `fetch.is_blocked()` never trusts HTTP status or size. It
(a) matches the explicit challenge markers (`"routeName":"captchaChallenge"`,
the Arabic title `كلمة التحقق`) **and** (b) *requires a positive real-content
marker* — `browserInitialState` or `"@type":"RealEstateListing"` — before
calling a fetch successful. Absence-of-bad-signal isn't enough; there has to be
proof of good signal. A block therefore becomes a logged `failures` row, never a
fat empty "success."

---

## 6. Dynamic JS injection: the description isn't in the raw HTML *or* in Algolia

**Problem.** The one field that matters most for Group B — the free-text
`description` — is **not in the Algolia payload** (confirmed by explicitly
requesting `attributesToRetrieve: ["*"]` plus `description` across sibling
indexes; the key never returns — see
[`data/network/VERDICT.md`](data/network/VERDICT.md)), and it's **not sitting in
static HTML** as clean text either. It's injected via the site's SPA framework.

**How I got past it.** I traced what the browser actually loads
([`probes/network_trace.py`](probes/network_trace.py)) and found the description
lives in the page's **server-rendered Redux state**: a `<script
id="browserInitialState">` blob whose `property.data.mainDescription` holds the
**verbatim** text with its `<br/>` markup intact — exactly the "full original
text, unedited" the brief wants. `parse.py` seeks to `"value":` in that blob and
`raw_decode`s the object out. This is more reliable than the DOM: it's the same
data the page renders from, captured before any client-side mutation.

---

## 7. Content-hashed CSS classes — the DOM is a moving target

**Problem.** Bayut's CSS class names are **content-hashed** (`._4dbf61ca`,
`.a0e18a1a`) and **rotate on every deploy**. Any selector anchored on them would
work today and silently break next week — the classic "scraper rots in
production" trap.

**How I got past it.** `parse.py` never anchors on a hashed class. Extraction
order is: (1) the SSR state blob (stable JSON keys, §6), (2) JSON-LD
`<script type="application/ld+json">` (a schema.org contract, tag-free text),
(3) a DOM fallback anchored on `aria-label="وصف العقار"` — an accessibility
attribute that describes meaning, not styling, and so is far more stable than a
hashed class.

---

## 8. LLM rate limits on the free tier — the extraction bottleneck

**Problem.** With no international credit card (I'm in Egypt), the extractor runs
on **Google AI Studio's free Gemini tier**, where the binding constraint is
requests-per-day/minute, and two traps bite:

- **Thinking models return empty JSON.** `gemini-3.6-flash` spends its output
  budget on internal reasoning first; with a normal token cap it burns the whole
  allowance thinking and returns **empty content** — a `JSONDecodeError` on line
  1. The lite models have **tiny daily caps** (`gemini-2.5-flash-lite` RPD 20,
  `gemini-3.6-flash` RPD 25) that exhaust almost immediately.
- Bursts trip a shared **~15 req/min** metric and 429 mid-run.

**How I got past it.**

- **Model choice is really a rate-limit choice**: settled on
  **`gemini-3.5-flash-lite`** (RPD **500**) — enough to do the whole ≥500-listing
  target in one day — after the tiny-cap and thinking models proved unworkable.
- **Roomy output cap** (8192) so a thinking model can't starve itself into an
  empty response.
- **Pace + retry**: a minimum inter-call interval keeps under the per-minute cap,
  and 12 retries **honor Google's own stated back-off** ("please retry in 24.9s")
  instead of guessing — so a 429 waits exactly as long as asked and recovers,
  rather than dropping the listing.
- **Nothing is lost on failure**: every extraction attempt is a row in the
  `extractions` cache keyed on `(listing_id, prompt_version, model)`; a failed
  call is re-runnable and the next run picks it up. The `any_model` resume logic
  even lets the tail finish on a *different* free model without re-doing rows an
  earlier model already extracted.

---

## The numbers (from `python cli.py failures`)

| Stage | `error_class` | Count | Nature |
|---|---|---|---|
| fetch | `blocked` | 13 | Humbucker challenges — caught by `is_blocked()`, listings stay pending and retry |
| fetch | `exception` | 22 | Nav timeouts / context teardown mid-run — logged, not swallowed; retried next run |
| extract | `llm_error` | 190 | Free-tier rate-limit / empty-response while settling on a working model — all recovered by retry/pacing |

**None of these lost data.** Every fetch failure leaves the listing in the
`pending_fetch` queue; every extract failure is a re-runnable `extractions` row.
The final dataset is **521 listings / 517 fully extracted**, and the failures
above are the visible, honest cost of getting there — surfaced in a table, not
hidden behind a fat 200 or a swallowed exception.
