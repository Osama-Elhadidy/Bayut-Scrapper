"""Stage: write the final dataset to XLSX -- the deliverable the brief asks
be inspectable without running anything. Column order: Group A, then Group
B, then derived. Nulls are written as empty cells, never the strings
"None"/"nan" -- someone opens this in Excel and judges null handling by
eye first."""

import json

import pandas as pd

from . import config, evaluate

GROUP_A_COLS = [
    "listing_id", "url", "purpose", "property_type", "price", "price_period",
    "currency", "bedrooms", "bathrooms", "area_sqm", "location_raw",
    "agency_name", "is_verified", "date_listed", "description_raw", "language",
]
GROUP_B_COLS = [
    "compound_name", "developer_name", "governorate", "city", "district",
    "finishing_level", "delivery_status", "delivery_date", "sale_type",
    "payment_type", "down_payment_amount", "down_payment_pct",
    "installment_years", "installment_amount", "installment_frequency",
    "cash_discount_pct", "amenities", "floor_number", "garden_area_sqm",
    "roof_area_sqm", "is_negotiable",
]
DERIVED_COLS = ["price_per_sqm", "total_installment_cost"]

FIELD_DICTIONARY = (
    [(c, "Group A (stated on page)") for c in GROUP_A_COLS]
    + [(c, "Group B (extracted from description)") for c in GROUP_B_COLS]
    + [(c, "Derived (computed)") for c in DERIVED_COLS]
)


def _records_dataframe(conn) -> pd.DataFrame:
    cols = GROUP_A_COLS + GROUP_B_COLS + DERIVED_COLS
    rows = conn.execute(f"SELECT {','.join(cols)} FROM records").fetchall()
    df = pd.DataFrame([dict(r) for r in rows], columns=cols)
    if "amenities" in df.columns:
        df["amenities"] = df["amenities"].apply(
            lambda v: ", ".join(json.loads(v)) if isinstance(v, str) and v else None)
    for boolcol in ("is_verified", "is_negotiable"):
        if boolcol in df.columns:
            df[boolcol] = df[boolcol].map({1: True, 0: False, None: None})
    return df


def export(conn, out_path=None, gold_path=None, log=print):
    out_path = out_path or config.OUT_XLSX
    out_path.parent.mkdir(parents=True, exist_ok=True)

    df = _records_dataframe(conn)

    dict_df = pd.DataFrame(FIELD_DICTIONARY, columns=["field", "source"])

    try:
        eval_results = evaluate.evaluate(conn, gold_path=gold_path, log=lambda *_: None)
        eval_rows = [{"field": f, **{k: v for k, v in r.items()}}
                     for f, r in eval_results.items()]
        eval_df = pd.DataFrame(eval_rows)
    except FileNotFoundError:
        eval_df = pd.DataFrame([{"note": "gold_25.csv not found -- run evaluate after labeling"}])

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="listings", index=False, na_rep="")
        dict_df.to_excel(writer, sheet_name="field_dictionary", index=False)
        eval_df.to_excel(writer, sheet_name="evaluation", index=False, na_rep="")

    log(f"wrote {len(df)} rows -> {out_path}")
    return len(df)
