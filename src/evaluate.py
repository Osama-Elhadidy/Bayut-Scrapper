"""
Stage: score the pipeline against a hand-labeled gold set.

Reads data/gold/gold_25.csv (listing_id + the Group B columns, hand-filled
by opening 25 real listings and reading them -- see README for the
labeling protocol) and joins to `records`. For every Group B field reports:

  gold_nonnull / pred_nonnull  -- how often each side had an opinion
  accuracy                     -- among rows where gold is non-null:
                                    numeric  = within tolerance
                                    enum/str = exact match after canonicalization
                                    amenities = Jaccard >= 0.5
  hallucination_rate           -- predicted non-null WHERE gold IS null,
                                    over all gold-null rows for that field.
                                    This is the number the brief cares about
                                    most: "how often did the pipeline invent
                                    a value where the truth was null."

Never reads anything the extractor tunes on -- this module only reads,
never writes, `records`.
"""

import csv
import json
import re

from . import config, normalize

GROUP_B_FIELDS = [
    "compound_name", "developer_name", "governorate", "city", "district",
    "finishing_level", "delivery_status", "delivery_date", "sale_type",
    "payment_type", "down_payment_amount", "down_payment_pct",
    "installment_years", "installment_amount", "installment_frequency",
    "cash_discount_pct", "amenities", "floor_number", "garden_area_sqm",
    "roof_area_sqm", "is_negotiable",
]

_NUMERIC = {"down_payment_amount", "down_payment_pct", "installment_years",
            "installment_amount", "cash_discount_pct", "garden_area_sqm",
            "roof_area_sqm"}
_TOLERANCE_PCT = 0.05  # 5% relative tolerance for numeric fields


def _is_null(v):
    return v is None or v == "" or (isinstance(v, float) and v != v)


def _numeric_match(gold, pred):
    try:
        gold, pred = float(gold), float(pred)
    except (TypeError, ValueError):
        return False
    if gold == 0:
        return pred == 0
    return abs(gold - pred) / abs(gold) <= _TOLERANCE_PCT


def _text_match(gold, pred):
    return normalize.clean_arabic(str(gold).strip().lower()) == \
        normalize.clean_arabic(str(pred).strip().lower())


def _amenities_match(gold, pred):
    g = {normalize.clean_arabic(x.strip().lower()) for x in (gold or []) if x}
    p = {normalize.clean_arabic(x.strip().lower()) for x in (pred or []) if x}
    if not g and not p:
        return True
    union = g | p
    if not union:
        return True
    return len(g & p) / len(union) >= 0.5


def load_gold(path=None):
    path = path or config.GOLD_CSV
    rows = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            lid = row["listing_id"].strip()
            rows[lid] = row
    return rows


def evaluate(conn, gold_path=None, log=print):
    gold = load_gold(gold_path)
    if not gold:
        log("gold set is empty")
        return {}

    results = {}
    for field in GROUP_B_FIELDS:
        results[field] = dict(gold_nonnull=0, pred_nonnull=0, correct=0,
                                gold_null=0, hallucinated=0)

    rows_evaluated = 0
    for lid, gold_row in gold.items():
        rec = conn.execute("SELECT * FROM records WHERE listing_id=?", (lid,)).fetchone()
        if rec is None:
            log(f"  {lid}: not in records (never fetched/parsed/extracted) -- skipped")
            continue
        rows_evaluated += 1
        for field in GROUP_B_FIELDS:
            gold_val = gold_row.get(field, "")
            gold_val = None if gold_val is None or gold_val.strip() == "" else gold_val.strip()
            pred_val = rec[field]
            if field == "amenities":
                # Gold cells list amenities separated by comma or semicolon
                # (whichever the labeler used); split on either so the set
                # comparison sees individual amenities, not one fused string.
                gold_val = ([s.strip() for s in re.split(r"[;,]", gold_val)]
                            if gold_val else None)
                pred_val = json.loads(pred_val) if pred_val else None

            r = results[field]
            gold_null = _is_null(gold_val) if field != "amenities" else not gold_val
            pred_null = _is_null(pred_val) if field != "amenities" else not pred_val

            if not gold_null:
                r["gold_nonnull"] += 1
            else:
                r["gold_null"] += 1
            if not pred_null:
                r["pred_nonnull"] += 1

            if gold_null and not pred_null:
                r["hallucinated"] += 1
            elif not gold_null and not pred_null:
                if field == "amenities":
                    match = _amenities_match(gold_val, pred_val)
                elif field in _NUMERIC:
                    match = _numeric_match(gold_val, pred_val)
                else:
                    match = _text_match(gold_val, pred_val)
                if match:
                    r["correct"] += 1

    log(f"evaluated {rows_evaluated}/{len(gold)} gold rows (rest not yet in pipeline)")
    for field, r in results.items():
        r["accuracy"] = (r["correct"] / r["gold_nonnull"]) if r["gold_nonnull"] else None
        r["hallucination_rate"] = (r["hallucinated"] / r["gold_null"]) if r["gold_null"] else None
    return results


def to_markdown(results: dict) -> str:
    lines = ["| field | gold non-null | pred non-null | accuracy | hallucination rate |",
             "|---|---|---|---|---|"]
    for field, r in results.items():
        acc = f"{r['accuracy']:.0%}" if r["accuracy"] is not None else "n/a"
        hal = f"{r['hallucination_rate']:.0%}" if r["hallucination_rate"] is not None else "n/a"
        lines.append(f"| {field} | {r['gold_nonnull']} | {r['pred_nonnull']} | {acc} | {hal} |")
    return "\n".join(lines)
