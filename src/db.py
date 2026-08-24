"""
All pipeline state lives in one SQLite file. Every stage reads its input
from a table here and writes its output back to a table here -- nothing
passes between stages in memory. That single rule is what makes the
pipeline resumable and idempotent: re-running any stage just re-issues the
same "what's pending" query, which naturally shrinks to zero as rows fill
in, and every write goes through upsert() so a re-run can never create a
duplicate row.

PRAGMA journal_mode=WAL so the DB can be queried from another terminal
(e.g. `sqlite3 data/bayut.db "select * from failures"`) while a fetch is
running.
"""

import json
import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    listing_id     TEXT PRIMARY KEY,
    url            TEXT NOT NULL,
    purpose        TEXT,
    governorate    TEXT,
    category       TEXT,
    hit_json       TEXT NOT NULL,
    discovered_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pages (
    listing_id    TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    html          TEXT NOT NULL,
    bytes         INTEGER,
    content_hash  TEXT,
    fetched_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- One row per listing. Group A + Group B + derived fields, all nullable --
-- null is a correct, expected answer for most Group B columns on most rows.
CREATE TABLE IF NOT EXISTS records (
    listing_id             TEXT PRIMARY KEY,
    url                    TEXT,
    purpose                TEXT,
    property_type          TEXT,
    price                  REAL,
    price_period            TEXT,
    currency                TEXT,
    bedrooms                INTEGER,
    bathrooms               INTEGER,
    area_sqm                 REAL,
    location_raw             TEXT,
    agency_name              TEXT,
    is_verified              INTEGER,
    date_listed              TEXT,
    description_raw          TEXT,
    language                 TEXT,

    compound_name             TEXT,
    developer_name            TEXT,
    governorate               TEXT,
    city                      TEXT,
    district                  TEXT,
    finishing_level           TEXT,
    delivery_status           TEXT,
    delivery_date             TEXT,
    sale_type                 TEXT,
    payment_type              TEXT,
    down_payment_amount       REAL,
    down_payment_pct          REAL,
    installment_years         REAL,
    installment_amount        REAL,
    installment_frequency     TEXT,
    cash_discount_pct         REAL,
    amenities                 TEXT,   -- JSON array
    floor_number               TEXT,
    garden_area_sqm            REAL,
    roof_area_sqm               REAL,
    is_negotiable                INTEGER,

    price_per_sqm             REAL,
    total_installment_cost    REAL,

    -- Structured hints from Bayut's own Algolia index (NOT the description).
    -- Bayut tracks a few Group-B-shaped attributes internally as real form
    -- fields (completionStatus, downPayment, ownership, furnishingStatus,
    -- amenities) even though they aren't rendered as filterable page
    -- elements. Used only as a grounding/backfill signal in extract.py --
    -- never as a silent substitute for reading the description -- and
    -- documented as such in the README.
    hint_completion_status  TEXT,
    hint_ownership          TEXT,
    hint_down_payment       REAL,
    hint_furnishing_status  TEXT,
    hint_amenities          TEXT,   -- JSON array
    hint_compound_name      TEXT,   -- JSON-LD breadcrumb level 3, if present
    -- Developer payment plan, straight from Bayut's extraFields (off-plan
    -- listings only). Exact structured numbers, not prose -- backfilled onto
    -- installment_amount / installment_years in extract.py.
    hint_monthly_installment  REAL,
    hint_installment_years    REAL,

    parsed_at            TEXT,
    parsed_content_hash  TEXT,
    extracted_at       TEXT,
    normalized_at        TEXT,
    extraction_model      TEXT,
    prompt_version         TEXT,
    taxonomy_version        TEXT
);

-- Audit trail / cache for every LLM call: never re-spend on an unchanged
-- (listing_id, prompt_version, model) triple.
CREATE TABLE IF NOT EXISTS extractions (
    listing_id    TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    raw_json       TEXT,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    cost_usd       REAL,
    grounded       INTEGER,
    error          TEXT,
    ts             TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (listing_id, prompt_version, model)
);

-- Dead-letter log. Every failure in every stage lands here instead of being
-- swallowed; this table is the source for the failure-log deliverable.
CREATE TABLE IF NOT EXISTS failures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id   TEXT,
    stage        TEXT NOT NULL,
    error_class  TEXT,
    message      TEXT NOT NULL,
    ts           TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_failures_stage ON failures(stage, error_class);
"""


def _migrate_legacy_tables(conn):
    """This project reuses a database that already had `pages` and
    `failures` tables from an earlier prototype, with a slightly different
    column set (no content_hash, no error_class/message split). Both were
    effectively empty of anything worth keeping (0 fetched pages, a handful
    of debug rows), so rather than a real ALTER TABLE migration, a schema
    mismatch on either table just drops and recreates it. `listings` (3000+
    already-discovered rows, real Algolia data, expensive to reproduce) is
    never touched here."""
    for table, required_col in (("pages", "content_hash"), ("failures", "message")):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and required_col not in cols:
            conn.execute(f"DROP TABLE {table}")

    # `records` gains columns over time (new hint_* backfill sources). Unlike
    # pages/failures it holds expensive, hard-to-reproduce data, so it's never
    # dropped -- new nullable columns are added in place. ADD COLUMN with a
    # default of NULL is instant and safe on an existing table.
    rec_cols = {r["name"] for r in conn.execute("PRAGMA table_info(records)")}
    if rec_cols:  # table already exists from an earlier run
        for col, decl in (("hint_monthly_installment", "REAL"),
                          ("hint_installment_years", "REAL")):
            if col not in rec_cols:
                conn.execute(f"ALTER TABLE records ADD COLUMN {col} {decl}")
    conn.commit()


def connect(db_path: Path = None) -> sqlite3.Connection:
    db_path = db_path or config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate_legacy_tables(conn)
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, table, row: dict, key_cols):
    """INSERT ... ON CONFLICT(key) DO UPDATE. The one write path every
    stage uses -- re-running a stage over rows it already touched updates
    them in place rather than duplicating or erroring."""
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    col_list = ",".join(cols)
    if isinstance(key_cols, str):
        key_cols = [key_cols]
    update_cols = [c for c in cols if c not in key_cols]
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    sql = (f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
           f"ON CONFLICT({','.join(key_cols)}) DO UPDATE SET {update_clause}")
    conn.execute(sql, [row[c] for c in cols])


def log_failure(conn, listing_id, stage, message, error_class=None):
    conn.execute(
        "INSERT INTO failures (listing_id, stage, error_class, message) "
        "VALUES (?,?,?,?)",
        (listing_id, stage, error_class, str(message)[:2000]))
    conn.commit()


# ---------------------------------------------------------------- resume

# Discovery ran twice with different configs, so the governorate column
# holds both Arabic slugs and English names for the same three provinces.
# Collapse them to one canonical label so stratification buckets line up.
_GOV_CANON = {
    "القاهرة": "Cairo", "الجيزة": "Giza",
    "الاسكندرية": "Alexandria", "الإسكندرية": "Alexandria",
    "Cairo": "Cairo", "Giza": "Giza", "Alexandria": "Alexandria",
}


def _canon_gov(name):
    return _GOV_CANON.get((name or "").strip(), (name or "").strip() or "Unknown")


def pending_fetch(conn, limit=None, stratify=True):
    """Discovered but not fetched, excluding listings that failed fetch for
    a non-transient reason too many times. Captcha/block failures ARE
    retried (a later run may have a fresh session); repeated hard errors on
    the same listing are not retried forever.

    stratify=True (default) round-robins the returned slice evenly across
    every (governorate, purpose) bucket instead of draining one bucket at a
    time. The brief requires >=500 listings spanning >=3 governorates and
    BOTH purposes; ordering by discovered_at fetched 500 straight Cairo-sale
    pages and satisfied none of that. Round-robin makes any --limit N a
    balanced sample by construction, and because fetch is the slow,
    bot-gated stage, the composition of the FIRST 500 fetched is exactly
    what ends up in the dataset."""
    base = """
        SELECT l.listing_id, l.url, l.governorate, l.purpose FROM listings l
        LEFT JOIN pages p ON p.listing_id = l.listing_id
        WHERE p.listing_id IS NULL
          AND l.listing_id NOT IN (
              SELECT listing_id FROM failures
              WHERE stage='fetch' AND error_class NOT IN ('blocked','timeout')
              GROUP BY listing_id HAVING COUNT(*) >= 3)
        ORDER BY l.discovered_at
    """
    rows = list(conn.execute(base))
    # Dedup: the same listing_id can appear under both an Arabic and an
    # English governorate slice. Keep the first seen (stable order).
    seen, uniq = set(), []
    for r in rows:
        if r["listing_id"] in seen:
            continue
        seen.add(r["listing_id"])
        uniq.append(r)

    if not stratify:
        out = [(r["listing_id"], r["url"]) for r in uniq]
        return out[:int(limit)] if limit else out

    buckets = {}
    for r in uniq:
        key = (_canon_gov(r["governorate"]), r["purpose"])
        buckets.setdefault(key, []).append((r["listing_id"], r["url"]))

    ordered, keys = [], sorted(buckets)
    idx = {k: 0 for k in keys}
    remaining = True
    while remaining:
        remaining = False
        for k in keys:
            i = idx[k]
            if i < len(buckets[k]):
                ordered.append(buckets[k][i])
                idx[k] += 1
                remaining = True
                if limit and len(ordered) >= int(limit):
                    return ordered
    return ordered


def pending_parse(conn, limit=None):
    """Fetched but not yet parsed into `records`, OR the page's content
    changed since the last parse (content_hash mismatch) -- a re-fetch of a
    listing that legitimately changed should re-parse, not be skipped
    forever."""
    sql = """
        SELECT p.listing_id, p.html FROM pages p
        LEFT JOIN records r ON r.listing_id = p.listing_id
        WHERE r.listing_id IS NULL
           OR r.parsed_at IS NULL
           OR r.parsed_content_hash IS NOT p.content_hash
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(r["listing_id"], r["html"]) for r in conn.execute(sql)]


def pending_extract(conn, prompt_version=None, model=None, limit=None,
                    any_model=True):
    """records with a description to mine that have no successful extraction
    yet. Bumping PROMPT_VERSION in config.py invalidates old extractions and
    puts rows back in this queue -- nothing is deleted, the old row just stops
    matching.

    any_model=True (default): a record counts as done once ANY model has
    extracted it at the current prompt_version. This is what lets the run be
    finished across several free models -- e.g. gemini-3.5-flash-lite for the
    first 500/day and gemini-3.1-flash-lite for the tail -- without
    re-extracting rows an earlier model already did. Set any_model=False to
    scope "done" to one specific model (e.g. to re-run everything on a better
    model for a clean single-model comparison)."""
    prompt_version = prompt_version or config.PROMPT_VERSION
    model = model or config.EXTRACT_MODEL
    if any_model:
        done_clause = "WHERE prompt_version = ? AND error IS NULL"
        done_params = [prompt_version]
    else:
        done_clause = "WHERE prompt_version = ? AND model = ? AND error IS NULL"
        done_params = [prompt_version, model]
    sql = f"""
        SELECT r.listing_id, r.description_raw, r.language FROM records r
        WHERE r.description_raw IS NOT NULL
          AND r.listing_id NOT IN (
              SELECT listing_id FROM extractions {done_clause})
    """
    params = list(done_params)
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [(r["listing_id"], r["description_raw"], r["language"])
            for r in conn.execute(sql, params)]


def all_records(conn):
    return conn.execute("SELECT * FROM records").fetchall()
