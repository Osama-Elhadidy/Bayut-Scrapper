"""
Stage 2: fetch detail-page HTML for discovered listings.

THIS IS THE STAGE THAT ACTUALLY FIGHTS BOT DETECTION. bayut.eg runs
Humbucker (an in-house bot wall) in front of every HTML page, its own
`/api/` namespace, and even its `/mcp` AI-agent endpoint -- all three
answer a cold request with `Www-Authenticate: hb-challenge, hb-captcha`
and a redirect to /captchaChallenge (hCaptcha). The ONE thing not behind
it is the Algolia search index (a different host), which is why discovery
is free and fetch is the whole fight. Findings from live testing:

  plain httpx / curl_cffi (any TLS fingerprint, even with session cookies)
      -> 200 + an 833KB /captchaChallenge page. Humbucker needs live JS
         execution; cookies alone never clear it.

  Playwright + BUNDLED Chromium + stealth
      -> client-side reload loop; page.content() throws mid-navigation.
         The bundled binary is itself the fingerprint.

  Playwright + REAL installed Chrome (channel="chrome") + stealth
      + a homepage warm-up + human-paced delays
      -> clears the JS challenge cleanly. This is the working recipe.

Two efficiency choices, both of which also lower the challenge surface:

  1. READ THE BODY OFF THE WIRE, not page.content(). The document response
     is captured from the `response` event the moment it arrives and is
     immutable; page.content() re-serializes the LIVE DOM and can throw if
     the page reloads mid-call -- exactly the bundled-Chromium failure.

  2. AGGRESSIVE ROUTE BLOCKING. The SSR HTML already contains everything we
     need (`__STRAT_SERVER_STATE__` + JSON-LD), so of ~300 sub-requests a
     listing page fires we allow only the document itself and Bayut's own
     Humbucker challenge scripts (which MUST run, or we guarantee a
     challenge) -- and abort every image, font, stylesheet, analytics
     vendor and app bundle. ~300 requests/listing -> a handful.

The honest limitation (README "what breaks first"): this is velocity- and
reputation-scored per IP. A burst of ~15-20 automated navigations from one
address can tip every subsequent request into a challenge, homepage
included. The lever is pacing, not a cleverer bypass -- long human-scale
delays, small batches, and a circuit breaker that STOPS the run the moment
even a warm-up navigation is challenged, rather than burning the queue.
`is_blocked()` keeps that failure loud (a `failures` row) instead of silent
(an empty-looking success on a fat 200).
"""

import asyncio
import hashlib
import json
import random
import re

from . import config, db

# Allowed through: the document navigation, and Bayut's own Humbucker
# challenge scripts (hb.bayut.eg / /.humbucker/). Everything else is aborted.
_BLOCK_TYPES = {"image", "font", "media", "stylesheet"}
_HUMBUCKER_RE = re.compile(r"(\.humbucker/|//hb\.bayut\.eg/|/captchaChallenge)", re.I)
_BAYUT_HOST_RE = re.compile(r"(^|\.)bayut\.eg$", re.I)


def is_blocked(html: str) -> bool:
    """Content marker, not an HTTP-status check -- the challenge response is
    HTTP 200 with a large body (obfuscated JS), so response.ok and a size
    check both pass on a page with zero listing data. Check explicit
    challenge markers first, then require a positive real-content marker
    (SSR state blob or JSON-LD listing) rather than trusting
    absence-of-bad-signal alone."""
    if not html or len(html) < 1000:
        return True
    if '"routeName":"captchaChallenge"' in html or "كلمة التحقق" in html[:3000]:
        return True
    if ("browserInitialState" not in html
            and '"@type":"RealEstateListing"' not in html):
        return True
    return False


def _should_block(request) -> bool:
    """Abort everything that is not the document or a Humbucker script."""
    if request.resource_type == "document":
        return False
    if _HUMBUCKER_RE.search(request.url):
        return False
    if request.resource_type in _BLOCK_TYPES:
        return True
    try:
        host = request.url.split("/")[2]
    except IndexError:
        return True
    # Keep Bayut's own first-party fetch/xhr (the SPA sometimes needs one to
    # settle the challenge); drop every third-party host and all app bundles.
    if _BAYUT_HOST_RE.search(host) and request.resource_type in ("fetch", "xhr"):
        return False
    return True


async def _make_context(browser, stealth, use_session=True):
    """Load saved storage_state (cookies + localStorage) when present.
    Cookies alone don't clear the JS challenge, but a context carrying a
    device_id / analytics history is a meaningfully warmer signal than a
    cold one, and it's free continuity. storage_state is re-saved after
    every run so session history accumulates across runs."""
    kw = dict(
        locale="ar-EG", timezone_id="Africa/Cairo",
        viewport={"width": 1440, "height": 900},
        extra_http_headers={"Accept-Language": "ar,en;q=0.9"},
    )
    if use_session and config.SESSION_FILE.exists():
        kw["storage_state"] = str(config.SESSION_FILE)
    ctx = await browser.new_context(**kw)
    await stealth.apply_stealth_async(ctx)
    return ctx


def extract_state(html: str):
    """Pull `__STRAT_SERVER_STATE__` out of the SSR blob. The script tag is
    not JSON -- it is
    `Object.defineProperty(window,"__STRAT_SERVER_STATE__",{"value":{...}})`
    -- so seek to `"value":` and let raw_decode find the object's end."""
    m = re.search(r'<script id="browserInitialState"[^>]*>(.*?)</script>',
                  html, re.S)
    if not m:
        return None
    raw = m.group(1)
    i = raw.find('"value":')
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[i + len('"value":'):])
        return obj
    except ValueError:
        return None


def description_from_state(state):
    """`mainDescription` is verbatim text with its <br /> markup intact --
    exactly what the spec's 'full original text, unedited' asks for. Used
    only as a fetch-time sanity signal here; parse.py re-derives it."""
    data = ((state or {}).get("property") or {}).get("data") or {}
    return data.get("mainDescription") or data.get("description") or None


async def fetch_pages(conn, limit=500, headless=False, block=True, log=print):
    """One continuous browser session for the whole batch (never relaunch
    per listing -- a cold process landing on a deep listing URL is itself a
    signal). Human-paced delays with jitter between listings. A
    consecutive-block streak triggers escalating backoff and eventually
    stops the run rather than hammering a score-based detector, which only
    lowers the score further."""
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    todo = db.pending_fetch(conn, limit)
    if not todo:
        log("nothing pending")
        return 0
    log(f"fetching {len(todo)} pages (headless={headless}, block={block})")

    stealth = Stealth()
    ok = bad = streak = 0
    counts = {"allowed": 0, "blocked": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless, channel=config.BROWSER_CHANNEL)
        ctx = await _make_context(browser, stealth)

        if block:
            async def router(route, request):
                if _should_block(request):
                    counts["blocked"] += 1
                    try:
                        await route.abort()
                    except Exception:
                        pass
                else:
                    counts["allowed"] += 1
                    try:
                        await route.continue_()
                    except Exception:
                        pass
            await ctx.route("**/*", router)

        page = await ctx.new_page()

        # Capture document bodies off the wire, keyed by URL so a redirect to
        # /captchaChallenge lands under its own key and can't be mistaken for
        # the listing HTML.
        bodies = {}

        async def on_response(resp):
            try:
                if resp.request.resource_type == "document":
                    bodies[resp.url] = await resp.text()
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        # Warm-up: Humbucker sets trust cookies on first contact; landing cold
        # on a deep listing URL with no session is itself a signal.
        try:
            await page.goto("https://www.bayut.eg/", wait_until="domcontentloaded",
                            timeout=45000)
            await page.wait_for_timeout(config.WARMUP_WAIT_MS)
            warmup_html = "".join(bodies.values()) or await page.content()
        except Exception as e:
            log(f"warm-up navigation failed: {e!r}")
            warmup_html = ""

        # Circuit breaker: if the homepage itself is under challenge, the
        # session has no trust to spend. Stop now rather than walking the
        # whole queue into `failures` one by one.
        if is_blocked(warmup_html):
            log("warm-up page itself is challenged -- session has no trust "
                "right now. Not attempting any listings. Run `session` to "
                "clear a challenge by hand, or wait and retry later.")
            await browser.close()
            return 0

        for i, (lid, url) in enumerate(todo, 1):
            bodies.clear()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(
                    config.NAV_WAIT_MS + random.randint(0, 1200))

                html = next((b for u, b in bodies.items()
                             if "captchaChallenge" not in u), None)
                if html is None:
                    html = await page.content()

                if is_blocked(html):
                    db.log_failure(conn, lid, "fetch", "content marker matched",
                                   error_class="blocked")
                    streak += 1
                    bad += 1
                    log(f"  [{i}/{len(todo)}] {lid} BLOCKED (streak {streak})")
                    if streak >= config.MAX_CONSECUTIVE_BLOCKS:
                        log(f"  {streak} consecutive blocks -- stopping. Re-run "
                            f"later; already-fetched pages are untouched.")
                        break
                    await page.wait_for_timeout(min(20000 * streak, 120000))
                    continue

                streak = 0
                content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
                db.upsert(conn, "pages", dict(
                    listing_id=lid, url=url, html=html, bytes=len(html),
                    content_hash=content_hash), key_cols="listing_id")
                conn.commit()
                ok += 1
                if i == 1 or i % 10 == 0:
                    state = extract_state(html)
                    desc = description_from_state(state) if state else None
                    log(f"  [{i}/{len(todo)}] {lid} ok {len(html)//1024}KB  "
                        f"desc={len(desc) if desc else 0}ch")

            except Exception as e:
                db.log_failure(conn, lid, "fetch", repr(e), error_class="exception")
                bad += 1
                log(f"  [{i}/{len(todo)}] {lid} ERROR {e!r}")
                if "has been closed" in str(e) or "crashed" in str(e).lower():
                    log(f"  browser/context is gone -- stopping this batch "
                        f"({len(todo) - i} listings left for next run)")
                    break

            await asyncio.sleep(random.uniform(config.FETCH_MIN_DELAY,
                                               config.FETCH_MAX_DELAY))

        try:
            config.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            await ctx.storage_state(path=str(config.SESSION_FILE))
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass

    log(f"fetched {ok}, blocked/failed {bad}")
    if block and (counts["allowed"] + counts["blocked"]):
        per = (counts["allowed"] + counts["blocked"]) / max(len(todo), 1)
        log(f"requests: {counts['allowed']} allowed / {counts['blocked']} blocked "
            f"(~{per:.0f}/listing seen, ~{counts['allowed']/max(len(todo),1):.0f} kept)")
    return ok


async def establish_session(log=print):
    """One-time, human-in-the-loop bootstrap: open a real, visible Chrome
    window, browse normally and clear any challenge by hand, then save
    cookies. Not a bypass -- this is what a legitimate returning visitor
    looks like. Useful as a manual reset when the circuit breaker trips and
    the automated warm-up can't clear the challenge on its own."""
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    stealth = Stealth()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel=config.BROWSER_CHANNEL)
        ctx = await _make_context(browser, stealth)
        page = await ctx.new_page()
        await page.goto("https://www.bayut.eg/ar/")
        log("Browse a listing or two by hand, clear any challenge, then press Enter here.")
        input("  > ")
        config.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(config.SESSION_FILE))
        log(f"saved -> {config.SESSION_FILE}")
        await browser.close()
