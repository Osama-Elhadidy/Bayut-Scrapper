"""
Network trace: what does bayut.eg ACTUALLY call when you browse القاهرة?

The point is to find a legitimate data endpoint the browser itself hits that
carries `description` -- so we can stop paying the full-page-render cost (and
its bot challenge) for 500 listings.

Two traces, because they can differ sharply:

  A. COLD LOAD of the Cairo for-sale search page. Every request logged.
  B. IN-APP CLICK from that search page into a listing card. This is the
     interesting one: bayut runs an in-house SPA framework ("strat"), and if
     client-side routing fetches listing data as JSON, that request is a
     data endpoint -- far cheaper and less challenge-prone than a cold deep
     navigation to the detail URL, which is what fetch.py does today.

Then the decisive step: read the description out of the rendered DOM, take a
distinctive 40-char slice of it, and grep EVERY captured response body for
that slice. Whichever file contains it is the file that carries the
description. No guessing.

Everything is written to data/network/ so you can read the bodies yourself:
  data/network/requests.csv   -- one row per request (url, type, status, bytes)
  data/network/bodies/        -- every text/json body, verbatim
  data/network/VERDICT.md     -- what carries the description

Run:  python network_trace.py
      python network_trace.py --headless      (expect challenges; see fetch.py)
"""

import asyncio
import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# This script lives in probes/, so the repo root (holding src/ and data/) is
# one level up -- put THAT on sys.path and anchor data/ there.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from src import config  # noqa: E402

OUT = _ROOT / "data" / "network"
BODIES = OUT / "bodies"

# Cairo, for sale. Verified live: the province slug is bare `القاهرة` with
# NO `محافظة-` prefix and NO `/ar/` segment -- both variants 404 (confirmed,
# an 880KB soft-404 that still renders the search shell, so it looks like a
# working page until you check the status code). Read off the site's own
# `api/areaGuideLink` response: `locationSlug=/القاهرة/...`.
CAIRO_SEARCH = "https://www.bayut.eg/القاهرة/عقارات-للبيع/"

# Bodies we never need to keep -- images, fonts, the analytics firehose.
SKIP_TYPES = {"image", "font", "media", "stylesheet"}
SKIP_HOST = re.compile(
    r"(google|doubleclick|facebook|hotjar|segment|amplitude|sentry|"
    r"cloudflareinsights|gstatic|googletagmanager|adservice|criteo|"
    r"clarity\.ms|newrelic)", re.I)


def slugify(url: str, n: int) -> str:
    u = urlparse(url)
    tail = (u.path.rsplit("/", 1)[-1] or "root")[:60]
    tail = re.sub(r"[^A-Za-z0-9._-]", "_", tail)
    h = hashlib.sha1(url.encode()).hexdigest()[:6]
    return f"{n:03d}_{u.netloc.split('.')[0][:14]}_{tail}_{h}"


class Trace:
    """Collects every response body worth keeping, tagged by phase."""

    def __init__(self):
        self.rows = []
        self.bodies = {}      # filename -> text
        self.phase = "A_search"
        self.n = 0

    def attach(self, page):
        page.on("response", lambda r: asyncio.create_task(self._on(r)))

    async def _on(self, resp):
        try:
            req = resp.request
            url = resp.url
            rtype = req.resource_type
            if rtype in SKIP_TYPES or SKIP_HOST.search(url):
                return
            self.n += 1
            ctype = (resp.headers.get("content-type") or "").split(";")[0]
            body = None
            if any(k in ctype for k in ("json", "html", "javascript", "text")):
                try:
                    body = await resp.text()
                except Exception:
                    body = None

            name = ""
            # Skip the giant JS bundles -- they're code, not data.
            if body and not (rtype == "script" and len(body) > 400_000):
                name = slugify(url, self.n) + (".json" if "json" in ctype else ".txt")
                self.bodies[name] = body

            self.rows.append(dict(
                phase=self.phase, n=self.n, method=req.method, status=resp.status,
                resource_type=rtype, content_type=ctype,
                bytes=len(body) if body else 0, body_file=name, url=url,
                post_data=(req.post_data or "")[:2000],
            ))
        except Exception:
            pass

    def flush(self):
        OUT.mkdir(parents=True, exist_ok=True)
        BODIES.mkdir(parents=True, exist_ok=True)
        for name, text in self.bodies.items():
            (BODIES / name).write_text(text, encoding="utf-8", errors="replace")
        with open(OUT / "requests.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=[
                "phase", "n", "method", "status", "resource_type",
                "content_type", "bytes", "body_file", "url", "post_data"])
            w.writeheader()
            w.writerows(self.rows)


def describe(rows, phase, log):
    """Print the data-bearing requests -- XHR/fetch/document only, no noise."""
    interesting = [r for r in rows if r["phase"] == phase
                   and r["resource_type"] in ("xhr", "fetch", "document")]
    log(f"\n  {phase}: {len([r for r in rows if r['phase']==phase])} requests kept, "
        f"{len(interesting)} data-bearing (xhr/fetch/document):")
    for r in interesting:
        host = urlparse(r["url"]).netloc
        path = urlparse(r["url"]).path[:70]
        log(f"    [{r['status']}] {r['method']:4s} {r['resource_type']:8s} "
            f"{r['bytes']:>8,}B  {host}{path}")
    return interesting


async def extract_description(page):
    """Read the description straight out of the rendered DOM -- this is our
    needle. Anchored on aria-label, never on hashed class names."""
    for sel in ('div[aria-label="وصف العقار"]',
                'div[aria-label="Property description"]',
                '[aria-label*="وصف"]'):
        try:
            el = await page.query_selector(sel)
            if el:
                txt = (await el.inner_text()).strip()
                if len(txt) > 60:
                    return txt
        except Exception:
            continue
    return None


def hunt(needle, bodies, log):
    """Which captured body contains the description? Try several slices --
    the JSON copy may be escaped (\\u0634...) or whitespace-normalized, so a
    single long literal match is fragile."""
    core = re.sub(r"\s+", " ", needle).strip()
    probes = []
    for start in (0, len(core) // 3, len(core) // 2):
        frag = core[start:start + 40].strip()
        if len(frag) >= 20:
            probes.append(frag)
    # JSON-escaped form of the first probe (شقة...)
    if probes:
        probes.append("".join(f"\\u{ord(c):04x}" for c in probes[0][:20]))

    found = []
    for name, text in bodies.items():
        flat = re.sub(r"\s+", " ", text)
        for p in probes:
            if p in flat or p in text:
                found.append(name)
                break
    for name in found:
        log(f"    FOUND in {name}  ({len(bodies[name]):,} bytes)")
    if not found:
        log("    not found in ANY captured body")
    return found


async def main(headless=False):
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    log = print
    trace = Trace()
    stealth = Stealth()
    verdict = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless,
                                          channel=config.BROWSER_CHANNEL)
        kw = dict(locale="ar-EG", timezone_id="Africa/Cairo",
                  viewport={"width": 1440, "height": 900},
                  extra_http_headers={"Accept-Language": "ar,en;q=0.9"})
        if config.SESSION_FILE.exists():
            kw["storage_state"] = str(config.SESSION_FILE)
        ctx = await browser.new_context(**kw)
        await stealth.apply_stealth_async(ctx)
        page = await ctx.new_page()
        trace.attach(page)

        # ---- warm-up (not traced as a phase, but keeps the session sane)
        log("warm-up: homepage")
        trace.phase = "0_warmup"
        await page.goto("https://www.bayut.eg/", wait_until="domcontentloaded",
                        timeout=60000)
        await page.wait_for_timeout(config.WARMUP_WAIT_MS)

        # ---- A: cold load of the Cairo search page
        log(f"\n[A] cold load: {CAIRO_SEARCH}")
        trace.phase = "A_search"
        await page.goto(CAIRO_SEARCH, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        try:
            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(2500)
        except Exception:
            pass
        title = await page.title()
        log(f"  page title: {title}")
        if "كلمة التحقق" in title:
            log("  CHALLENGED on the search page -- trace will be thin. "
                "Run `python cli.py session` to clear it by hand first.")

        # ---- B: in-app click into a listing (SPA navigation, not a cold hit)
        log("\n[B] in-app click into the first listing card")
        trace.phase = "B_click"
        card = None
        for sel in ('a[aria-label="Listing link"]',
                    'article a[href*="/property/details-"]',
                    'a[href*="/property/details-"]'):
            card = await page.query_selector(sel)
            if card:
                log(f"  card selector that matched: {sel}")
                break
        listing_url = None
        if not card:
            log("  no listing card found in the DOM (challenged, or markup moved)")
        else:
            listing_url = await card.get_attribute("href")
            log(f"  clicking -> {listing_url}")
            try:
                await card.click()
                await page.wait_for_timeout(6000)
            except Exception as e:
                log(f"  click failed ({e!r}) -- falling back to direct goto")
                trace.phase = "B_goto"
                await page.goto("https://www.bayut.eg" + (listing_url or ""),
                                wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(5000)

        log(f"  landed on: {page.url}")
        desc = await extract_description(page)
        if desc:
            log(f"  description in DOM: {len(desc)} chars")
            log(f"    {desc[:140]}...")
        else:
            log("  no description in the DOM (challenged or not a detail page)")

        # ---- report
        log("\n" + "=" * 66)
        log("REQUESTS")
        log("=" * 66)
        for ph in ("0_warmup", "A_search", "B_click", "B_goto"):
            if any(r["phase"] == ph for r in trace.rows):
                describe(trace.rows, ph, log)

        log("\n" + "=" * 66)
        log("WHO CARRIES THE DESCRIPTION?")
        log("=" * 66)
        if desc:
            hits = hunt(desc, trace.bodies, log)
            for name in hits:
                row = next((r for r in trace.rows if r["body_file"] == name), None)
                if row:
                    verdict.append(f"- `{row['resource_type']}` {row['url'][:110]}"
                                   f"  ({row['bytes']:,} B) -> `{name}`")
        else:
            log("  no description captured, nothing to hunt for")

        trace.flush()
        try:
            await ctx.storage_state(path=str(config.SESSION_FILE))
        except Exception:
            pass
        await browser.close()

    (OUT / "VERDICT.md").write_text(
        "# Which network response carries `description`?\n\n"
        + ("\n".join(verdict) if verdict
           else "None of the captured responses contained the description text.\n")
        + "\n\nFull request log: `requests.csv`. Bodies: `bodies/`.\n",
        encoding="utf-8")
    log(f"\nwrote {len(trace.bodies)} bodies + requests.csv -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main(headless="--headless" in sys.argv))
