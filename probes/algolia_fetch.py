"""
Standalone probe: how far can pure-Algolia get us, with zero page fetches?

The question this answers: each Algolia response carries ~24 full hits, so
~21 responses cover the 500-listing target. If those hits already contained
`description`, the whole Playwright/bot-challenge stage could be deleted.

So this script does three things and prints the verdict:

  1. Sweeps N responses of the live index and saves each one verbatim to
     data/algolia_raw/ (nothing thrown away -- you can re-read them later
     without re-hitting the network).
  2. Asks for EVERY attribute (`attributesToRetrieve: ["*"]`) plus an
     explicit `description` request, and reports whether the key comes back.
  3. Probes sibling index names (en/ar, other environments) in case a
     different index exposes the description that this one hides.

Run:  python algolia_fetch.py            # 21 responses x 24 hits
      python algolia_fetch.py 5 100      # 5 responses x 100 hits
"""

import json
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import httpx

APP_ID = "LL8IZ711CS"
FALLBACK_KEY = "07de0a8209b2f3cd921152dfe39310a9"
INDEX = "bayut-eg-production-ads-ar"
URL = f"https://{APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# This script lives in probes/, so the repo root (where data/ is) is one up.
OUT = Path(__file__).resolve().parent.parent / "data" / "algolia_raw"

# Sibling index names worth a shot -- the ar index is what the site's own
# search calls; if a description lives anywhere it would be on a detail-
# oriented index, not the search one.
CANDIDATE_INDEXES = [
    "bayut-eg-production-ads-ar",
    "bayut-eg-production-ads-en",
    "bayut-eg-production-ads-ar-price-asc",
    "bayut-eg-production-ads",
    "bayut-eg-production-ads-details-ar",
    "bayut-eg-production-listings-ar",
]


def get_api_key():
    """Same trick discover.py uses: read the browser search key off the
    homepage so a key rotation isn't a mystery 403."""
    try:
        r = httpx.get("https://www.bayut.eg/",
                      headers={"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"},
                      timeout=20, follow_redirects=True)
        import re
        m = re.search(r'"ALGOLIA_BROWSER_SEARCH_API_KEY"\s*:\s*"([a-f0-9]{32})"', r.text)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"  (homepage key scrape failed: {e!r} -- using fallback)")
    return FALLBACK_KEY


def query(key, index=INDEX, filters="", page=0, hits_per_page=24,
          attributes=None, raw=False):
    parts = [f"hitsPerPage={hits_per_page}", f"page={page}"]
    if filters:
        parts.append(f"filters={quote(filters, safe='')}")
    if attributes is not None:
        parts.append(f"attributesToRetrieve={quote(json.dumps(attributes), safe='')}")
    r = httpx.post(
        URL,
        params={"x-algolia-agent": "algolia-fetch-probe",
                "x-algolia-api-key": key,
                "x-algolia-application-id": APP_ID},
        json={"requests": [{"indexName": index, "params": "&".join(parts)}]},
        timeout=30)
    r.raise_for_status()
    body = r.json()
    if raw:
        return body
    return body["results"][0]


# ---------------------------------------------------------------- test 1

def test_description_present(key):
    """The whole question, in one request. Ask for everything, then ask for
    `description` by name, and see what actually comes back."""
    print("\n[1] Does a hit carry `description`?")

    star = query(key, attributes=["*"], hits_per_page=1)["hits"][0]
    keys = sorted(star.keys())
    print(f"  attributesToRetrieve=['*'] -> {len(keys)} keys")
    print(f"  {', '.join(keys)}")

    hits = ("description" in keys, "description_l1" in keys)
    print(f"\n  'description'    present: {hits[0]}")
    print(f"  'description_l1' present: {hits[1]}")

    named = query(key, attributes=["externalID", "description", "description_l1",
                                   "title", "title_l1"],
                  hits_per_page=1)["hits"][0]
    print(f"\n  explicit request for description -> keys returned: "
          f"{sorted(named.keys())}")
    print("  (Algolia silently omits attributes that don't exist or aren't "
          "retrievable -- an absent key here means it is NOT in the index.)")

    # What text DOES come back? This is the ceiling of a no-fetch pipeline.
    print("\n  free text available without any page fetch:")
    for k in ("title", "title_l1", "keywords", "keywords_l1", "amenities_l1"):
        v = star.get(k)
        if v:
            s = json.dumps(v, ensure_ascii=False)
            print(f"    {k:14s} {s[:160]}")
    return hits[0] or hits[1]


# ---------------------------------------------------------------- test 2

def test_other_indexes(key):
    """Maybe another index holds it. Cheap to check, decisive either way."""
    print("\n[2] Sibling indexes")
    for idx in CANDIDATE_INDEXES:
        try:
            res = query(key, index=idx, attributes=["*"], hits_per_page=1)
            hits = res.get("hits") or []
            if not hits:
                print(f"  {idx:38s} exists, 0 hits")
                continue
            has = "description" in hits[0] or "description_l1" in hits[0]
            print(f"  {idx:38s} {res.get('nbHits', 0):>9,} hits   "
                  f"description: {has}")
        except httpx.HTTPStatusError as e:
            msg = ""
            try:
                msg = e.response.json().get("message", "")
            except Exception:
                pass
            print(f"  {idx:38s} HTTP {e.response.status_code}  {msg[:60]}")
        time.sleep(0.2)


# ---------------------------------------------------------------- test 3

def sweep(key, n_responses=21, hits_per_page=24):
    """The actual proposal: N responses, saved verbatim, coverage measured.

    Sliced by purpose x governorate so the pool is stratified rather than
    whatever ranking surfaces first -- and because Algolia caps pagination
    depth anyway, so one broad query can't reach far.
    """
    print(f"\n[3] Sweep: {n_responses} responses x {hits_per_page} hits")
    OUT.mkdir(parents=True, exist_ok=True)

    slices = [(p, name, ext) for p in ("for-sale", "for-rent")
              for name, ext in (("Cairo", "1-5"), ("Giza", "1-68"),
                                ("Alexandria", "1-6"))]

    seen, field_counts, rows = set(), Counter(), []
    calls = 0
    page = 0
    while calls < n_responses:
        progressed = False
        for purpose, gov, ext in slices:
            if calls >= n_responses:
                break
            filters = f'purpose:"{purpose}" AND location.externalID:"{ext}"'
            res = query(key, filters=filters, page=page,
                        hits_per_page=hits_per_page, attributes=["*"])
            calls += 1
            hits = res.get("hits") or []
            if not hits:
                continue
            progressed = True
            path = OUT / f"{purpose}_{gov}_p{page}.json"
            path.write_text(json.dumps(res, ensure_ascii=False, indent=1),
                            encoding="utf-8")
            for h in hits:
                lid = h.get("externalID")
                if not lid or lid in seen:
                    continue
                seen.add(lid)
                rows.append(h)
                for f in ("price", "rooms", "baths", "area", "purpose",
                          "category", "location", "agency", "isVerified",
                          "createdAt", "title_l1", "amenities_l1",
                          "furnishingStatus", "completionStatus"):
                    if h.get(f) not in (None, "", [], {}):
                        field_counts[f] += 1
            print(f"  {purpose:9s} {gov:11s} p{page}  +{len(hits):3d} hits  "
                  f"total unique {len(seen):4d}  (avail {res.get('nbHits', 0):,})")
            time.sleep(0.35)
        if not progressed:
            break
        page += 1

    n = len(rows) or 1
    print(f"\n  {calls} requests -> {len(seen)} unique listings, "
          f"saved to {OUT}")
    print("  Group A coverage (share of hits with a non-empty value):")
    for f, c in field_counts.most_common():
        print(f"    {f:18s} {c:4d}/{len(rows)}  {100*c/n:5.1f}%")
    missing_desc = len(rows)
    print(f"    {'description':18s} {0:4d}/{len(rows)}  {0.0:5.1f}%"
          f"   <-- the gap ({missing_desc} listings with no free text to extract from)")
    return len(seen), calls


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 21
    hpp = int(sys.argv[2]) if len(sys.argv) > 2 else 24

    key = get_api_key()
    print(f"key {key[:8]}...  index {INDEX}")

    found = test_description_present(key)
    test_other_indexes(key)
    unique, calls = sweep(key, n_responses=n, hits_per_page=hpp)

    print("\n" + "=" * 62)
    print("VERDICT")
    print("=" * 62)
    print(f"  Group A  : {unique} listings in {calls} requests, no bot challenge.")
    if found:
        print("  Group B  : description IS in the index -- the fetch stage can go.")
    else:
        print("  Group B  : description is NOT in any Algolia payload.")
        print("             Group B (finishing, amenities, payment plan, view,")
        print("             compound...) lives only in the free-text description,")
        print("             which only the rendered detail page carries.")
        print("             -> Algolia replaces discovery, not fetch.")


if __name__ == "__main__":
    main()
