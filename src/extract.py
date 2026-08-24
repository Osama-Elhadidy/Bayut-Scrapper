"""
Stage 4: Group B extraction. Rules first, LLM second -- the split that
matters most for defending this pipeline in the interview:

  Rules (normalize.py)  -- currency, percentages, digit-groups, year/quarter
                            tokens. Deterministic, so regex genuinely wins:
                            "1,500,000" and مليون ونصف always parse to the
                            same float, no model call needed or wanted.

  LLM (this module)     -- semantics. Which sentence is the down payment
                            vs. the installment amount vs. just the price
                            again; whether "متشطب بالكامل" describes THIS
                            unit or a neighbouring one; whether a compound
                            name is actually named or merely implied. This
                            is not something regex can disambiguate.

One listing per call, temperature 0, Pydantic schema with Literal enums.
Every numeric/date value the model returns is re-parsed through
normalize.py before being trusted -- the model is asked for the field, not
for arithmetic. A grounding check then verifies every non-null value is
actually findable in the source text; anything that isn't gets nulled and
logged, which is the highest-leverage function in the repo for the
20-point "honest null handling" section.

Caching: keyed on (listing_id, prompt_version, model) in `extractions`.
Re-running never re-spends on a row that's already cached for the current
prompt/model -- bump PROMPT_VERSION in config.py to invalidate deliberately.
"""

import json
import re
import time
from datetime import datetime, timezone

from . import config, db, normalize
from .schema import GroupBExtraction

# Anthropic first-party per-1M-token rates, used to estimate cost_usd from
# response usage when OpenRouter doesn't hand back a cost figure directly.
# Reconcile against the OpenRouter credits dashboard for the true total --
# provider-side token accounting can under/over-report versus what's billed.
_RATE_TABLE_USD_PER_1M = {
    "anthropic/claude-haiku-4.5": (1.0, 5.0),
    "anthropic/claude-sonnet-4.5": (3.0, 15.0),
    "anthropic/claude-opus-4.5": (5.0, 25.0),
    # Google AI Studio free tier -> $0. (These are the paid per-1M rates for
    # reference; via a free GOOGLE_API_KEY the actual cost is zero.)
    "google/gemini-3.6-flash": (0.0, 0.0),
    "google/gemini-3.5-flash-lite": (0.0, 0.0),
    "google/gemini-3.1-flash-lite": (0.0, 0.0),
    "google/gemini-2.5-flash-lite": (0.0, 0.0),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "google/gemini-2.0-flash-001": (0.10, 0.40),
    # ":free" variants cost $0 -- estimate stays 0 and that's correct.
    "google/gemini-2.0-flash-exp:free": (0.0, 0.0),
    "google/gemini-2.5-flash-lite:free": (0.0, 0.0),
}

SYSTEM_PROMPT = """You extract structured facts from Egyptian real-estate listing descriptions (Arabic, English, or mixed).

Return ONLY facts literally stated in the text, or unambiguously implied by it. If a field is not mentioned, its value is null -- null is the CORRECT answer for most fields on most listings. Do not guess, estimate, or fill in a plausible-sounding value. A missing field is far better than an invented one; a wrong guess is scored worse than a null.

Field notes:
- finishing_level: core & shell / semi-finished / fully finished / super lux / furnished / null. Arabic: تشطيب سوبر لوكس=super lux, نص تشطيب=semi-finished, على المحارة=core & shell, متشطب بالكامل=fully finished, مفروش=furnished.
- delivery_status: ready / off-plan. استلام فوري / جاهز = ready. تسليم <year> / تحت الانشاء = off-plan.
- delivery_date: only if a year or quarter is explicitly stated (e.g. "تسليم 2027" -> "2027", "Q1 2028" -> "2028-Q1"). Never infer a date from delivery_status alone.
- sale_type: primary (sold by/on behalf of the developer) vs resale (an existing owner reselling).
- payment_type: cash / installments / both, based on what payment options the text actually offers.
- down_payment_amount, installment_amount: return the number AS WRITTEN in the text (e.g. "1.5M", "مليون ونصف", "1,500,000") -- do not convert it yourself, that happens after your response.
- down_payment_pct, cash_discount_pct: same -- return as written (e.g. "10%", "10 في المية").
- installment_years: number of years the plan spans.
- installment_frequency: monthly / quarterly / annual -- how often installment_amount is paid.
- amenities: only amenities explicitly named in THIS text, as short English phrases.
- floor_number: as stated (can be text like "ground" / "الدور الارضي").
- garden_area_sqm, roof_area_sqm: numeric, only if explicitly given for this unit.
- is_negotiable: true only if negotiability is explicitly mentioned (e.g. "قابل للتفاوض").
- compound_name: the gated residential PROJECT/compound the unit sits in, if the text names one. It usually follows a marker word -- "كمبوند"/"كومباوند"/"compound", "مشروع"/"project", "في"/"بـ" + a project name -- and is a brand/proper name, NOT a city, district or street. Egyptian examples so you recognise the pattern (do NOT assume these; only extract what THIS text states): Palm Hills بالم هيلز, Mountain View ماونتن فيو, Madinaty مدينتي, Mivida ميفيدا, Taj City تاج سيتي, Hyde Park هايد بارك, Zed / Zed West زيد, Sodic, Marassi مراسي, Hacienda هاسيندا, Il Monte Galala, Uptown Cairo, Katameya كتامية, Al Rehab الرحاب, Sarai سراي, Bloomfields, Zayed أكتوبر/الشيخ زايد is a CITY not a compound. If only a city/district/street is mentioned, compound_name is null.
- developer_name: the real-estate DEVELOPER company if named (e.g. Palm Hills Developments, SODIC, Emaar, Talaat Moustafa/TMG, Ora, Mountain View, Hassan Allam). Only if the text names the developer; the selling agency is NOT the developer.

Example (Arabic, off-plan with installments):
Text: "شقة للبيع في كمبوند بالم هيلز تسليم 2027 تشطيب سوبر لوكس مقدم 10% والباقي على 8 سنين"
-> compound_name="Palm Hills", finishing_level="super lux", delivery_status="off-plan", delivery_date="2027", payment_type="installments", down_payment_pct="10%", installment_years=8, everything else not mentioned -> null.

Example (English, resale, cash only, nothing else mentioned):
Text: "Resale apartment, cash only, prime location, contact for details."
-> sale_type="resale", payment_type="cash", ALL OTHER FIELDS null. Do not invent a finishing level, a compound name, or amenities that are not in the text.

Respond with a single JSON object matching the given schema. Every key must be present; use null (or [] for amenities) when not mentioned."""


def _get_client():
    """One OpenAI-compatible client, pointed at whichever provider is active.
    Google AI Studio (free Gemini tier) and OpenRouter both speak the OpenAI
    chat API, so only the base URL and key differ."""
    from openai import OpenAI
    if config.EXTRACT_PROVIDER == "google":
        return OpenAI(base_url=config.GOOGLE_BASE_URL, api_key=config.GOOGLE_API_KEY)
    return OpenAI(base_url=config.OPENROUTER_BASE_URL, api_key=config.OPENROUTER_API_KEY)


def _resolve_model(model: str) -> str:
    """OpenRouter names models "google/gemini-2.5-flash"; the native Google
    endpoint wants the bare "gemini-2.5-flash". Strip the vendor prefix when
    talking to Google directly so the same EXTRACT_MODEL value works for both."""
    if config.EXTRACT_PROVIDER == "google" and "/" in model:
        return model.split("/", 1)[1]
    return model


def _response_format():
    """OpenRouter honors strict json_schema; Google's OpenAI-compat layer is
    happiest with plain json_object mode (the schema shape is spelled out in
    the system prompt either way, and every value is re-validated downstream)."""
    if config.EXTRACT_PROVIDER == "google":
        return {"type": "json_object"}
    schema = GroupBExtraction.model_json_schema()
    schema["additionalProperties"] = False
    return {"type": "json_schema",
            "json_schema": {"name": "group_b_extraction", "strict": True, "schema": schema}}


def _create_with_retry(client, model, description, log=print):
    """Call the API, retrying on 429 (rate limit) with backoff that honors
    the X-RateLimit-Reset header when present. New OpenRouter accounts are
    capped at ~10 req/min per model; the caller also paces to stay under
    that, but bursts and clock skew still produce the occasional 429 -- a
    transient like that should wait and retry, not drop the listing to a
    dead-letter row as if the description were unparseable."""
    from openai import APIStatusError, RateLimitError
    last = None
    for attempt in range(config.EXTRACT_MAX_RETRIES):
        try:
            return client.chat.completions.create(
                model=_resolve_model(model),
                temperature=config.EXTRACT_TEMPERATURE,
                max_tokens=config.EXTRACT_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": description},
                ],
                response_format=_response_format(),
            )
        except RateLimitError as e:
            last = e
            # Honor the provider's own stated delay. Google returns
            # "Please retry in 24.9s" in the 429 body; OpenRouter uses an
            # X-RateLimit-Reset header. Fall back to exponential backoff.
            wait = min(2 ** attempt * 5, 60)  # 5,10,20,40,60,60s
            stated = _google_retry_seconds(e) or _reset_wait_seconds(e)
            if stated is not None:
                wait = max(wait, min(stated + 1.0, 90))
            log(f"    rate-limited, waiting {wait:.0f}s "
                f"(attempt {attempt + 1}/{config.EXTRACT_MAX_RETRIES})")
            time.sleep(wait)
        except APIStatusError as e:
            # OpenRouter returns 402 for TWO different situations. One is
            # transient -- "in_flight_budget_exhausted": too much credit is
            # committed to in-flight requests right now; it clears once they
            # settle, and the response carries a Retry-After. Wait and retry.
            # The other is terminal -- genuinely out of credits -- which no
            # amount of waiting fixes, so re-raise it to stop the run loudly.
            if not _is_transient_402(e):
                raise
            last = e
            wait = _retry_after_seconds(e) or min(2 ** attempt * 15, 120)
            log(f"    in-flight budget full, waiting {wait:.0f}s "
                f"(attempt {attempt + 1}/{config.EXTRACT_MAX_RETRIES})")
            time.sleep(wait)
    raise last


def _google_retry_seconds(err):
    """Google phrases its 429 as '... Please retry in 24.972s' inside the
    message. Pull that number out so we wait exactly as long as it asks
    rather than guessing with backoff."""
    m = re.search(r"retry in ([\d.]+)\s*s", str(err))
    return float(m.group(1)) if m else None


def _is_transient_402(err):
    try:
        if getattr(err, "status_code", None) != 402:
            return False
        meta = err.response.json().get("error", {}).get("metadata", {})
        reason = str(meta.get("reason", "")) + str(meta.get("limit_source", ""))
        return "in_flight" in reason
    except Exception:
        return False


def _retry_after_seconds(err):
    try:
        meta = err.response.json().get("error", {}).get("metadata", {})
        ra = (meta.get("headers", {}) or {}).get("Retry-After")
        return float(ra) if ra is not None else None
    except Exception:
        return None


def _reset_wait_seconds(err):
    """Pull X-RateLimit-Reset (epoch ms) off a 429 and return seconds to
    wait, if the header is present and sane."""
    try:
        headers = err.response.json().get("error", {}).get(
            "metadata", {}).get("headers", {})
        reset_ms = float(headers.get("X-RateLimit-Reset"))
        return max(0.0, reset_ms / 1000.0 - time.time())
    except Exception:
        return None


def call_llm(description: str, model: str = None, log=print):
    """One structured-output call. Returns (parsed_dict, tokens_in, tokens_out,
    raw_json_text). Falls back to a plain JSON parse of the message content
    if the provider doesn't honor strict structured outputs."""
    model = model or config.EXTRACT_MODEL
    client = _get_client()
    resp = _create_with_retry(client, model, description, log=log)
    content = resp.choices[0].message.content
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        parsed = json.loads(content[start:end + 1])
    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", None) if usage else None
    tokens_out = getattr(usage, "completion_tokens", None) if usage else None
    return parsed, tokens_in, tokens_out, content


def _estimate_cost(model, tokens_in, tokens_out):
    if tokens_in is None or tokens_out is None:
        return None
    rates = _RATE_TABLE_USD_PER_1M.get(model)
    if not rates:
        return None
    in_rate, out_rate = rates
    return round(tokens_in / 1_000_000 * in_rate + tokens_out / 1_000_000 * out_rate, 6)


# --------------------------------------------------------- post-processing

_NUMERIC_FIELDS = ["down_payment_amount", "down_payment_pct", "installment_years",
                    "installment_amount", "cash_discount_pct",
                    "garden_area_sqm", "roof_area_sqm"]
_PCT_FIELDS = {"down_payment_pct", "cash_discount_pct"}
_ENUM_FIELDS = {
    "finishing_level": normalize.canon_finishing_level,
    "delivery_status": normalize.canon_delivery_status,
    "sale_type": normalize.canon_sale_type,
    "payment_type": normalize.canon_payment_type,
    "installment_frequency": normalize.canon_installment_frequency,
}

# The closed vocabularies the schema's Literal enums allow. A value that matches
# none of its glossary variants AND is not already a valid member is dropped to
# null -- a closed enum must never leak an off-list value (e.g. the model
# returning "ultra lux" for finishing, or "متساوية" for frequency). The glossary
# (normalize.canon_*) can legitimately miss a valid literal it has no Arabic/
# English variant for -- notably payment_type "both" -- so validity is checked
# against THIS set, not against whether the glossary matched. Kept in sync with
# schema.py by hand.
_VALID_ENUM = {
    "finishing_level": {"core & shell", "semi-finished", "fully finished",
                        "super lux", "furnished"},
    "delivery_status": {"ready", "off-plan"},
    "sale_type": {"primary", "resale"},
    "payment_type": {"cash", "installments", "both"},
    "installment_frequency": {"monthly", "quarterly", "annual"},
}


def canon_enum(field, val):
    """Canonicalize one closed-enum value to a schema-valid member, or null.
    Prefers the glossary mapping; falls back to the raw value only if it is
    already a valid literal; otherwise drops it to null. Shared by the live
    extractor (canonicalize) and the LLM-free backfill so both enforce the same
    closed vocabulary."""
    if val in (None, ""):
        return None
    return _ENUM_FIELDS[field](val) or (val if val in _VALID_ENUM[field] else None)


def canonicalize(raw: dict) -> dict:
    """Re-parse every value the model returned through the deterministic
    rules layer -- the model supplies the field, normalize.py supplies the
    number. Also re-snaps enum fields defensively in case the model drifted
    off the exact literal (e.g. returned "Super Lux" instead of "super lux")."""
    out = dict(raw)
    for f in _NUMERIC_FIELDS:
        val = out.get(f)
        if val is None:
            continue
        parsed = (normalize.parse_percentage(val) if f in _PCT_FIELDS
                  else normalize.parse_number(val))
        out[f] = parsed
    if out.get("delivery_date"):
        out["delivery_date"] = normalize.parse_delivery_date(str(out["delivery_date"])) \
            or out["delivery_date"]
    for f in _ENUM_FIELDS:
        if out.get(f):
            out[f] = canon_enum(f, out[f])
    return out


def _grounded(value, description_clean, kind) -> bool:
    """A number is grounded if it (or its integer form) appears somewhere
    in the digit-normalized text; an enum is grounded if any of its
    glossary variants appear. Free-text fields (compound_name,
    developer_name, floor_number) are grounded if the value's own text
    appears as a substring -- a cheap but real check against pure
    invention."""
    if value is None:
        return True  # nothing to ground
    if kind == "number":
        s = str(value)
        candidates = {s, s.rstrip("0").rstrip(".") if "." in s else s,
                      str(int(value)) if float(value).is_integer() else s}
        return any(c in description_clean for c in candidates)
    if kind == "text":
        v = normalize.clean_arabic(str(value).lower())
        if not v:
            return False
        if v in description_clean:
            return True
        # Cross-script case: the model often returns a compound/developer in
        # the OTHER language than the text ("Hyde Park" vs "هايد بارك"), which
        # a substring check can never match. When the value's script differs
        # from the description's dominant script, the substring test can't
        # verify it either way, so don't reject on that basis alone -- a
        # separate structural source (Algolia level 4) is the real check.
        value_has_latin = bool(re.search(r"[a-z]", v))
        desc_has_latin = bool(re.search(r"[a-z]", description_clean))
        desc_has_arabic = bool(re.search(r"[؀-ۿ]", description_clean))
        if value_has_latin and desc_has_arabic and not desc_has_latin:
            return True   # can't verify Latin value against Arabic-only text
        if not value_has_latin and desc_has_latin and not desc_has_arabic:
            return True   # can't verify Arabic value against Latin-only text
        return False
    return True


def grounding_check(canon: dict, description_raw: str) -> tuple[dict, list]:
    """Null out any field that fails grounding; return (grounded_dict,
    list_of_rejected_field_names) so the caller can log exactly what was
    stripped and why."""
    desc_norm = normalize.clean_arabic(normalize.to_ascii_digits(description_raw).lower())
    rejected = []
    out = dict(canon)
    for f in _NUMERIC_FIELDS:
        if out.get(f) is not None and not _grounded(out[f], desc_norm, "number"):
            rejected.append(f)
            out[f] = None
    for f in ("compound_name", "developer_name", "floor_number"):
        if out.get(f) and not _grounded(out[f], desc_norm, "text"):
            rejected.append(f)
            out[f] = None
    return out, rejected


# --------------------------------------------------- structural back-fill

# The hint_* columns extract.py overlays onto the LLM output. Read once here
# so the live extractor and the standalone backfill pull the identical set.
# `purpose` isn't a hint -- it gates the sale-only guard below.
STRUCTURAL_HINT_COLS = ("hint_compound_name", "hint_down_payment",
                        "hint_ownership", "hint_monthly_installment",
                        "hint_installment_years", "price", "purpose")

# Fields that only make sense for a SALE, nulled when purpose == "rent"
# (whether the value came from the LLM or a structural hint). sale_type
# (primary/resale) and the developer payment-plan fields are category errors
# on a lease. NOTE: down_payment_amount/pct are deliberately NOT here -- a
# rental deposit ("مقدم") is a real, labelable down payment (the gold set
# treats it as one, e.g. a deposit as a % of the monthly rent), so nulling it
# would contradict ground truth. price_period is the rental analogue, kept.
SALE_ONLY_FIELDS = ("sale_type", "installment_amount", "installment_frequency",
                    "installment_years", "cash_discount_pct",
                    "total_installment_cost")


def apply_structural_hints(fields: dict, hints) -> dict:
    """Overlay Bayut's OWN structured fields (from the Algolia hit, captured
    as hint_* columns at parse time) on top of the LLM's read of the prose.
    Structural data wins where Bayut publishes the fact itself; the LLM value
    stands everywhere Bayut is silent. Pure function, no I/O -- so the live
    extractor and the standalone `backfill` stage share one source of truth
    and can never drift. `hints` is a sqlite3.Row or dict with the columns in
    STRUCTURAL_HINT_COLS."""
    def h(k):
        try:
            return hints[k]
        except (KeyError, IndexError, TypeError):
            return None

    out = dict(fields)
    struct_compound = h("hint_compound_name")
    struct_dp = h("hint_down_payment")
    ownership = h("hint_ownership")
    monthly = h("hint_monthly_installment")
    years = h("hint_installment_years")
    price = h("price")
    purpose = h("purpose")

    # compound_name: Algolia location level 4 is authoritative when present;
    # the LLM value stands only where there's no level-4 entry.
    if struct_compound:
        out["compound_name"] = struct_compound

    # sale_type: extraFields.ownership ("primary"/"resale") is the listing's
    # own declaration of who's selling -- trust it over the LLM's read of the
    # prose, which often can't tell developer-sale from owner-resale.
    if ownership:
        st = normalize.canon_sale_type(ownership)
        if st:
            out["sale_type"] = st

    # Developer payment plan: Bayut publishes monthly_installment and
    # installments_payment_duration_years as exact structured numbers on
    # off-plan listings. When present they beat whatever the prose spelled
    # out (and imply a monthly frequency). Absent -> the LLM value stands.
    if monthly not in (None, 0, 0.0):
        out["installment_amount"] = float(monthly)
        out["installment_frequency"] = "monthly"
    if years not in (None, 0, 0.0):
        out["installment_years"] = float(years)

    # down_payment_amount: structured `downPayment` figure ("الدفعة المقدمة").
    # The description rarely states an absolute amount, so the LLM returns
    # null/0 -- fill from the real figure, then derive the pct exactly.
    if out.get("down_payment_amount") in (None, 0, 0.0):
        out["down_payment_amount"] = (
            float(struct_dp) if struct_dp not in (None, 0, 0.0) else None)
    if (out.get("down_payment_pct") in (None, 0, 0.0)
            and out.get("down_payment_amount") and price):
        out["down_payment_pct"] = round(
            out["down_payment_amount"] / price * 100, 2)

    # total_installment_cost from whatever we now hold (structural or LLM) --
    # null unless the full plan (down payment + amount + frequency + years)
    # is present, so a partial plan never gets a guessed completion.
    out["total_installment_cost"] = normalize.derive_total_installment_cost(
        out.get("down_payment_amount"), out.get("installment_amount"),
        out.get("installment_frequency"), out.get("installment_years"))

    # Sale-only guard, applied LAST so it overrides both the LLM and the
    # structural fills: a rental has no sale_type or purchase-plan figures.
    if str(purpose).lower() == "rent":
        for f in SALE_ONLY_FIELDS:
            out[f] = None
    return out


# ------------------------------------------------------------------ driver

def extract_pending(conn, limit=None, model=None, log=print):
    model = model or config.EXTRACT_MODEL
    todo = db.pending_extract(conn, model=model, limit=limit)
    if not todo:
        log("nothing pending")
        return 0
    active_key = (config.GOOGLE_API_KEY if config.EXTRACT_PROVIDER == "google"
                  else config.OPENROUTER_API_KEY)
    if not active_key:
        keyname = ("GOOGLE_API_KEY" if config.EXTRACT_PROVIDER == "google"
                   else "OPENROUTER_API_KEY")
        log(f"{keyname} not set -- see .env.example. Nothing extracted.")
        return 0
    log(f"provider={config.EXTRACT_PROVIDER}  model={model}  "
        f"pace={config.EXTRACT_MIN_INTERVAL_S}s/call")

    ok = bad = total_cost = 0.0
    interval = config.EXTRACT_MIN_INTERVAL_S
    last_call = 0.0
    for lid, description, language in todo:
        # Pace calls to stay under the provider's requests/minute cap (new
        # OpenRouter accounts are throttled per model). Cheap insurance on top
        # of the 429 retry in call_llm.
        wait = interval - (time.monotonic() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.monotonic()
        try:
            raw, tin, tout, raw_json = call_llm(description, model=model, log=log)
            canon = canonicalize(raw)
            grounded, rejected = grounding_check(canon, description)
            cost = _estimate_cost(model, tin, tout)
            total_cost += cost or 0

            # Structural back-fills from Bayut's own data (captured at parse
            # time), applied AFTER the LLM+grounding so they win over the
            # model's read of the prose where Bayut publishes the fact itself.
            hint_row = conn.execute(
                f"SELECT {','.join(STRUCTURAL_HINT_COLS)} "
                "FROM records WHERE listing_id=?", (lid,)).fetchone()
            if hint_row:
                grounded = apply_structural_hints(grounded, hint_row)

            record = {"listing_id": lid, **{k: grounded.get(k) for k in
                       GroupBExtraction.model_fields.keys()}}
            record["amenities"] = json.dumps(grounded.get("amenities") or [],
                                              ensure_ascii=False)
            record["is_negotiable"] = (int(grounded["is_negotiable"])
                                        if grounded.get("is_negotiable") is not None else None)
            record["extracted_at"] = datetime.now(timezone.utc).isoformat()
            record["extraction_model"] = model
            record["prompt_version"] = config.PROMPT_VERSION
            record["taxonomy_version"] = config.TAXONOMY_VERSION

            # total_installment_cost was computed inside apply_structural_hints
            # from the final (structural-or-LLM) plan values.
            record["total_installment_cost"] = grounded.get("total_installment_cost")
            record["normalized_at"] = record["extracted_at"]

            db.upsert(conn, "records", record, key_cols="listing_id")

            db.upsert(conn, "extractions", dict(
                listing_id=lid, prompt_version=config.PROMPT_VERSION, model=model,
                raw_json=raw_json, tokens_in=tin, tokens_out=tout, cost_usd=cost,
                grounded=1 if not rejected else 0, error=None,
            ), key_cols=["listing_id", "prompt_version", "model"])
            conn.commit()
            ok += 1
            if rejected:
                log(f"  {lid}: grounding rejected {rejected}")
        except Exception as e:
            db.log_failure(conn, lid, "extract", repr(e), error_class="llm_error")
            db.upsert(conn, "extractions", dict(
                listing_id=lid, prompt_version=config.PROMPT_VERSION, model=model,
                raw_json=None, tokens_in=None, tokens_out=None, cost_usd=None,
                grounded=None, error=str(e)[:2000],
            ), key_cols=["listing_id", "prompt_version", "model"])
            conn.commit()
            bad += 1
            log(f"  {lid} ERROR {e!r}")

    log(f"extracted {ok}, failed {bad}, cost ~${total_cost:.4f}")
    return ok
