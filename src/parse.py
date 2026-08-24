"""
Stage 3: turn (Algolia hit_json, raw HTML) into Group A fields +
description_raw -- the raw material stage 4 (extract.py) runs the LLM over.

Two independent sources, used for what each is good at:

  hit_json (Algolia)  -- price, purpose, category, rooms/baths, area,
                          agency, verification, dates, and the FULL
                          bilingual governorate/city/district hierarchy,
                          already normalized by Bayut itself. No gazetteer,
                          no fuzzy matching needed for location.

  raw HTML             -- description_raw. Confirmed absent from the
                          Algolia payload entirely. Order of attack:
                            1. JSON-LD <script type="application/ld+json">,
                               @graph[0].mainEntity.description -- clean
                               plain text, no HTML tags, no CTA buttons.
                            2. DOM div[aria-label="وصف العقار"] -- same
                               text with <br> tags, used only if JSON-LD is
                               missing or clearly truncated. Anchored on
                               aria-label, never on the hashed class names
                               (._4dbf61ca etc.) that change every deploy.

A sanity gate: if price or purpose can't be recovered, the row is logged
as a parse failure rather than let a silently-empty record into the
dataset.
"""

import html as html_module
import json
import re
from datetime import datetime, timezone

from . import db, normalize

PROPERTY_TYPE_MAP = {
    "apartment": "apartment", "villa": "villa", "chalet": "chalet",
    "townhouse": "townhouse", "town house": "townhouse",
    "duplex": "duplex", "penthouse": "penthouse", "studio": "studio",
    "land": "land", "twin house": "villa", "twinhouse": "villa",
}


def _hierarchy(hit):
    by_level = {l["level"]: l for l in (hit.get("location") or [])
                if isinstance(l, dict) and "level" in l}
    g, c, d = by_level.get(1), by_level.get(2), by_level.get(3)
    return {
        "governorate": (g or {}).get("name_l1"),
        "city": (c or {}).get("name_l1"),
        "district": (d or {}).get("name_l1"),
        "location_raw": " > ".join(
            l["name"] for l in (hit.get("location") or [])
            if isinstance(l, dict) and l.get("level", 0) > 0 and l.get("name")
        ) or None,
    }


def _property_type(hit):
    cats = [c for c in (hit.get("category") or []) if isinstance(c, dict)]
    if not cats:
        return "other"
    deepest = max(cats, key=lambda c: c.get("level", 0))
    name = (deepest.get("nameSingular_l1") or deepest.get("name_l1") or "").lower()
    return PROPERTY_TYPE_MAP.get(name.strip(), "other")


def _epoch_to_date(ts):
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def group_a_from_hit(hit: dict) -> dict:
    """Everything derivable from the Algolia hit alone -- no HTML needed."""
    purpose_raw = hit.get("purpose")
    purpose = {"for-sale": "sale", "for-rent": "rent"}.get(purpose_raw, purpose_raw)
    property_type = _property_type(hit)
    rooms = hit.get("rooms")
    bedrooms = 0 if property_type == "studio" else (
        int(rooms) if isinstance(rooms, (int, float)) else None)
    agency = hit.get("agency") or {}
    hier = _hierarchy(hit)

    extra = hit.get("extraFields") or {}
    ownership = extra.get("ownership")

    return {
        "purpose": purpose,
        "property_type": property_type,
        "price": hit.get("price"),
        "price_period": hit.get("rentFrequency") if purpose == "rent" else None,
        "currency": "EGP",
        "bedrooms": bedrooms,
        "bathrooms": int(hit["baths"]) if isinstance(hit.get("baths"), (int, float)) else None,
        "area_sqm": hit.get("area"),
        "location_raw": hier["location_raw"],
        "agency_name": agency.get("name_l1") or agency.get("name"),
        "is_verified": 1 if hit.get("isVerified") else 0,
        "date_listed": _epoch_to_date(hit.get("createdAt")),
        "governorate": hier["governorate"],
        "city": hier["city"],
        "district": hier["district"],
        "hint_completion_status": hit.get("completionStatus"),
        "hint_ownership": ownership,
        "hint_down_payment": hit.get("downPayment"),
        "hint_furnishing_status": hit.get("furnishingStatus"),
        "hint_amenities": json.dumps(hit.get("amenities_l1") or [], ensure_ascii=False),
        # Developer payment plan, published by Bayut as structured numbers on
        # off-plan listings (absent on resale/cash listings -> null, correct).
        "hint_monthly_installment": extra.get("monthly_installment"),
        "hint_installment_years": extra.get("installments_payment_duration_years"),
    }


_LDJSON_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_DOM_DESC_RE = re.compile(
    r'aria-label="وصف العقار"[^>]*>\s*<div[^>]*>\s*<span[^>]*>(.*?)</span>', re.S)
_STATE_RE = re.compile(
    r'<script id="browserInitialState"[^>]*>(.*?)</script>', re.S)


def _clean_description(text: str) -> str:
    """<br /> -> newline BEFORE stripping tags, so the text keeps its line
    structure (evidence spans in extraction cross line breaks). Never
    collapse or edit further -- 'full original text, unedited'."""
    t = re.sub(r"<br\s*/?>", "\n", text)
    t = re.sub(r"<[^>]+>", "", t)
    return html_module.unescape(t).strip()


def _description_from_state(html_text: str):
    """Best source (FIELD_MAP): __STRAT_SERVER_STATE__.property.data
    .mainDescription is the verbatim listing text with <br/> markup intact.
    JSON-LD carries a tag-stripped copy, so prefer this when present.
    Returns (description, state_dict) so the caller can also mine structural
    hints (compound, ownership) straight from the state tree."""
    m = _STATE_RE.search(html_text)
    if not m:
        return None, None
    raw = m.group(1)
    i = raw.find('"value":')
    if i < 0:
        return None, None
    try:
        state, _ = json.JSONDecoder().raw_decode(raw[i + len('"value":'):])
    except ValueError:
        return None, None
    data = ((state or {}).get("property") or {}).get("data") or {}
    desc = data.get("mainDescription") or data.get("description")
    return (_clean_description(desc) if desc else None), state


def _description_from_ldjson(html_text: str) -> str | None:
    for m in _LDJSON_RE.finditer(html_text):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        graph = data.get("@graph") if isinstance(data, dict) else None
        if not graph:
            continue
        for node in graph:
            main = node.get("mainEntity") if isinstance(node, dict) else None
            if isinstance(main, dict) and main.get("description"):
                return main["description"], node
    return None, None


def _description_from_dom(html_text: str) -> str | None:
    m = _DOM_DESC_RE.search(html_text)
    if not m:
        return None
    raw = m.group(1)
    raw = re.sub(r"<br\s*/?>", "\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    return html_module.unescape(raw).strip()


# "كومباوند هايد بارك" / "Taj City Compound" -> strip the wrapper word so the
# stored name is the bare compound ("Hyde Park...", "Taj City"). Case- and
# script-tolerant; leaves the rest of the name untouched.
_COMPOUND_WRAPPER_RE = re.compile(
    r"\b(compound|project)\b|كومباوند|كمبوند|كمباوند|مشروع", re.IGNORECASE)


def _clean_compound(name: str) -> str | None:
    if not name:
        return None
    n = _COMPOUND_WRAPPER_RE.sub(" ", name)
    n = re.sub(r"\s+", " ", n).strip(" -،,")
    return n or name.strip()


def _compound_from_location(hit: dict) -> str | None:
    """Bayut's own normalized compound name, straight from the Algolia
    location hierarchy. Level 4 (below governorate/city/district) is the
    gated compound/project when the listing sits in one -- authoritative and
    free, no LLM guess. English (name_l1) preferred for a clean canonical
    label, Arabic as fallback. This is the primary source for compound_name;
    the description LLM only fills the listings that have no level-4 entry."""
    for l in hit.get("location") or []:
        if isinstance(l, dict) and l.get("level") == 4:
            return _clean_compound(l.get("name_l1") or l.get("name"))
    return None


def _compound_name_hint(ldjson_node: dict) -> str | None:
    """Fallback only: the JSON-LD breadcrumb's deepest crumb. Unreliable --
    it is often just the district -- so it is used only when the Algolia
    location hierarchy has no level-4 compound."""
    if not ldjson_node:
        return None
    crumbs = (ldjson_node.get("breadcrumb") or {}).get("itemListElement") or []
    if len(crumbs) >= 4:
        return crumbs[3].get("name")
    return None


def parse_one(lid: str, html_text: str, hit: dict) -> dict | None:
    row = {"listing_id": lid, "url": hit.get("_bayut_url") or None}
    row.update(group_a_from_hit(hit))

    # Description source order (best first): the verbatim SSR state blob,
    # then JSON-LD (tag-stripped copy), then the DOM span. JSON-LD is still
    # mined for the compound-name breadcrumb hint even when the state blob
    # supplied the text.
    desc, _state = _description_from_state(html_text)
    ldjson_desc, ldjson_node = _description_from_ldjson(html_text)
    if not desc:
        desc = ldjson_desc or _description_from_dom(html_text)
    row["description_raw"] = desc
    row["language"] = normalize.detect_language(desc) if desc else None
    # Compound from Bayut's structured location hierarchy (Algolia level 4) --
    # authoritative and clean. The JSON-LD breadcrumb fallback is deliberately
    # NOT used: it returns listing slugs like "1016-oAfbpC - بيوت", not
    # compound names. Listings without a level-4 entry get their compound from
    # the description LLM instead.
    row["hint_compound_name"] = _compound_from_location(hit)

    if row.get("price") is None or row.get("purpose") is None:
        return None  # sanity gate -- caller logs this as a parse failure

    # price_per_sqm only needs Group A (price, area_sqm), so it's computed
    # here rather than waiting on the LLM extraction stage -- a listing
    # with a hidden area still gets every OTHER field, just not this one.
    row["price_per_sqm"] = normalize.derive_price_per_sqm(row.get("price"), row.get("area_sqm"))
    return row


def parse_pending(conn, limit=None, log=print):
    todo = db.pending_parse(conn, limit)
    if not todo:
        log("nothing pending")
        return 0
    ok = bad = 0
    for lid, html_text in todo:
        hit_row = conn.execute(
            "SELECT hit_json, url FROM listings WHERE listing_id=?", (lid,)).fetchone()
        page_row = conn.execute(
            "SELECT content_hash FROM pages WHERE listing_id=?", (lid,)).fetchone()
        if not hit_row:
            db.log_failure(conn, lid, "parse", "no listings row", error_class="missing_hit")
            bad += 1
            continue
        hit = json.loads(hit_row["hit_json"])
        hit["_bayut_url"] = hit_row["url"]
        record = parse_one(lid, html_text, hit)
        if record is None:
            db.log_failure(conn, lid, "parse", "price or purpose missing after parse",
                            error_class="sanity_gate")
            bad += 1
            continue
        record["url"] = hit_row["url"]
        record["parsed_at"] = datetime.now(timezone.utc).isoformat()
        record["parsed_content_hash"] = page_row["content_hash"] if page_row else None
        db.upsert(conn, "records", record, key_cols="listing_id")
        conn.commit()
        ok += 1
    log(f"parsed {ok}, failed {bad}")
    return ok
