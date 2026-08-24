"""
Stage: apply structural back-fills to records that were already extracted,
WITHOUT re-calling the LLM.

Bayut publishes a handful of Group-B-shaped facts as its own structured
fields in the Algolia hit (extraFields.ownership, monthly_installment,
installments_payment_duration_years, downPayment; location level 4 for the
compound). extract.py already overlays these on fresh extractions via
extract.apply_structural_hints(). But a run that was extracted BEFORE a new
structural source was wired in would never see it -- and re-extracting 500+
listings just to pick up a free, deterministic field would waste LLM quota
for zero new information.

So this stage re-applies the identical apply_structural_hints() to the rows
already in `records`, reading the structured values straight from the stored
hit_json. It is:

  * LLM-free      -- pure data already on disk; costs nothing, hits no API.
  * idempotent    -- structural values are authoritative and stable, so a
                     second run overwrites each field with the same value.
  * non-destructive -- only fields Bayut actually publishes are touched; where
                     Bayut is silent the existing (LLM) value is left exactly
                     as-is. null stays null.

It also persists the two newest hint columns (hint_monthly_installment,
hint_installment_years) onto old records so the DB is self-consistent for any
future full re-extract.
"""

import json
from datetime import datetime, timezone

from . import db, extract
from .schema import GroupBExtraction

# Fields backfill can change -- the only ones we write back, so nothing else in
# the record is ever disturbed. Covers the structural-hint targets (+ the
# rent-guarded cash_discount_pct) and the closed-enum fields re-canonicalized
# below (finishing_level, delivery_status, payment_type; sale_type and
# installment_frequency already appear as structural targets).
_TOUCHED = ("compound_name", "sale_type", "installment_amount",
            "installment_frequency", "installment_years",
            "down_payment_amount", "down_payment_pct", "cash_discount_pct",
            "total_installment_cost",
            "finishing_level", "delivery_status", "payment_type")

_GROUP_B = tuple(GroupBExtraction.model_fields.keys())


def backfill(conn, limit=None, log=print):
    sql = """
        SELECT r.*, l.hit_json
        FROM records r JOIN listings l ON l.listing_id = r.listing_id
        WHERE r.extracted_at IS NOT NULL
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    if not rows:
        log("nothing to backfill (no extracted records)")
        return 0

    changed = 0
    counts = {f: 0 for f in _TOUCHED}
    for rec in rows:
        lid = rec["listing_id"]
        try:
            extra = (json.loads(rec["hit_json"]) or {}).get("extraFields") or {}
        except (TypeError, ValueError):
            extra = {}

        hints = {
            "hint_compound_name": rec["hint_compound_name"],
            "hint_down_payment": rec["hint_down_payment"],
            # ownership hint may predate this column; fall back to the hit.
            "hint_ownership": rec["hint_ownership"] or extra.get("ownership"),
            "hint_monthly_installment": extra.get("monthly_installment"),
            "hint_installment_years": extra.get("installments_payment_duration_years"),
            "price": rec["price"],
            "purpose": rec["purpose"],
        }

        current = {k: rec[k] for k in _GROUP_B}
        # total_installment_cost isn't a schema field; seed it so a re-derive
        # to the same value doesn't register as a spurious change.
        current["total_installment_cost"] = rec["total_installment_cost"]
        updated = extract.apply_structural_hints(current, hints)

        # Deterministic enum re-canonicalization (LLM-free): drop any closed-enum
        # value that isn't schema-valid -- e.g. an "ultra lux" / "متساوية" leak
        # from an older extraction -- and re-snap drifted casings.
        for ef in extract._VALID_ENUM:
            updated[ef] = extract.canon_enum(ef, updated.get(ef))

        diff = {f: updated.get(f) for f in _TOUCHED
                if updated.get(f) != current.get(f)}
        for f in diff:
            counts[f] += 1

        # Always persist the two structured plan columns (cheap, keeps the DB
        # consistent) even when they don't change a derived field.
        writeback = dict(diff)
        writeback["hint_monthly_installment"] = extra.get("monthly_installment")
        writeback["hint_installment_years"] = extra.get(
            "installments_payment_duration_years")

        if diff:
            writeback["normalized_at"] = datetime.now(timezone.utc).isoformat()
            changed += 1

        writeback["listing_id"] = lid
        db.upsert(conn, "records", writeback, key_cols="listing_id")
        conn.commit()

    log(f"backfill: {changed}/{len(rows)} records updated")
    for f, n in counts.items():
        if n:
            log(f"  {f}: {n} filled/changed")
    return changed
