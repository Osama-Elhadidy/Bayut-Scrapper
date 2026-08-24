"""
Stage 1: discover listing IDs + Group A metadata via Bayut's own Algolia
search index.

This index is public, unauthenticated, and carries no bot detection --
verified live (facets/verify_values below hit it directly with no
challenge). It hands back almost every Group A field per hit (price, rooms,
baths, area, agency, verification, full bilingual location hierarchy)
directly as structured JSON. The one field it withholds is `description`
(requesting it explicitly returns the key simply absent, not null) --
that's the reason a separate fetch stage against the rendered HTML exists
at all.

Sliced by purpose x governorate x category rather than one broad query:
Algolia caps pagination depth (~1000 hits) so a single query can't reach
far anyway, and slicing guarantees an evenly stratified pool across both
purposes and all three governorates instead of whatever the ranking
surfaces first.
"""

import json
import re
import time
from urllib.parse import quote

import httpx

from . import config, db


def get_api_key():
    """Scrape the current search key from the homepage rather than
    hardcoding it -- costs one request and turns key rotation into a
    non-event instead of a confusing 403 mid-run."""
    try:
        r = httpx.get(
            "https://www.bayut.eg/",
            headers={"User-Agent": config.UA, "Accept-Language": "ar,en;q=0.9"},
            timeout=20, follow_redirects=True)
        m = re.search(r'"ALGOLIA_BROWSER_SEARCH_API_KEY"\s*:\s*"([a-f0-9]{32})"',
                      r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return config.ALGOLIA_FALLBACK_KEY


def algolia(key, filters="", page=0, hits_per_page=100, facets=None):
    """One Algolia multi-query request. `quote(..., safe='')` matters: Algolia
    filters are a URL-encoded string nested inside a JSON body, and filter
    values here contain spaces, quotes and Arabic -- unencoded, the filter
    silently fails to parse and Algolia returns HTTP 200 with nbHits: 0. No
    exception, no warning, just a quietly empty result set."""
    parts = [f"hitsPerPage={hits_per_page}", f"page={page}"]
    if filters:
        parts.append(f"filters={quote(filters, safe='')}")
    if facets:
        parts.append(f"facets={quote(json.dumps(facets), safe='')}")
        parts.append("maxValuesPerFacet=100")

    r = httpx.post(
        config.ALGOLIA_URL,
        params={"x-algolia-agent": "strat-bayut-eg-prod-fec/b614a2b5",
                "x-algolia-api-key": key,
                "x-algolia-application-id": config.ALGOLIA_APP_ID},
        json={"requests": [{"indexName": config.ALGOLIA_INDEX,
                             "params": "&".join(parts)}]},
        timeout=30)
    r.raise_for_status()
    return r.json()["results"][0]


def verify_values(key, log=print):
    """Check each governorate/category filter actually matches something
    before sweeping, and drop the ones that don't. This is the guard
    against a whole class of silent failure: a stale externalID now prints
    a loud "-> 0" instead of the sweep quietly running empty slices and
    reporting a fake "0 new listings" as if that were a legitimate
    result."""
    good_gov, good_cat = {}, []
    for name, ext_id in config.GOVERNORATES.items():
        n = algolia(key, filters=f'purpose:"for-sale" AND location.externalID:"{ext_id}"',
                    hits_per_page=0)["nbHits"]
        log(f"  location.externalID {name:12s} ({ext_id}) -> {n:,}")
        if n:
            good_gov[name] = ext_id
    for c in config.CATEGORIES:
        n = algolia(key, filters=f'purpose:"for-sale" AND category.slug:"{c}"',
                    hits_per_page=0)["nbHits"]
        log(f"  category.slug {c:14s} -> {n:,}")
        if n:
            good_cat.append(c)
    return good_gov, good_cat


def save_hits(conn, hits, purpose, gov, cat):
    rows = []
    for h in hits:
        lid = h.get("externalID")
        if not lid:
            continue
        rows.append(dict(
            listing_id=str(lid),
            url=f"https://www.bayut.eg/ar/property/details-{lid}.html",
            purpose=purpose, governorate=gov, category=cat,
            hit_json=json.dumps(h, ensure_ascii=False),
        ))
    for row in rows:
        # INSERT OR IGNORE semantics: discovery never overwrites an existing
        # listing row (fetch/parse/extract state hangs off listing_id, and
        # re-discovering the same listing must be a pure no-op).
        conn.execute(
            "INSERT OR IGNORE INTO listings "
            "(listing_id,url,purpose,governorate,category,hit_json) "
            "VALUES (:listing_id,:url,:purpose,:governorate,:category,:hit_json)",
            row)
    conn.commit()
    return len(rows)


def discover(conn, per_slice=200, delay=0.4, log=print):
    key = get_api_key()
    log(f"key {key[:8]}...  index {config.ALGOLIA_INDEX}")

    baseline = algolia(key, filters='purpose:"for-sale"', hits_per_page=0)["nbHits"]
    log(f"baseline purpose=for-sale -> {baseline:,}")
    if not baseline:
        log("baseline is 0 -- key or index name is stale, aborting")
        return 0

    log("verifying filter values against the live index:")
    govs, cats = verify_values(key, log=log)
    if not govs or not cats:
        log("no usable governorate/category values -- aborting")
        return 0

    total = 0
    for purpose in config.PURPOSES:
        for gov_name, gov_id in govs.items():
            for cat in cats:
                filters = (f'purpose:"{purpose}" '
                           f'AND location.externalID:"{gov_id}" '
                           f'AND category.slug:"{cat}"')
                got = page = nb = 0
                while got < per_slice:
                    try:
                        res = algolia(key, filters=filters, page=page)
                    except Exception as e:
                        db.log_failure(conn, None, "discover", repr(e),
                                        error_class="http_error")
                        break
                    hits = res.get("hits", [])
                    nb = res.get("nbHits", 0)
                    if not hits:
                        break
                    total += save_hits(conn, hits, purpose, gov_name, cat)
                    got += len(hits)
                    page += 1
                    if page >= res.get("nbPages", 0):
                        break
                    time.sleep(delay)
                log(f"  {purpose:9s} {gov_name:10s} {cat:13s} "
                    f"got={got:4d} avail={nb:,}")

    log(f"new listings this run: {total}")
    return total
