"""
Pick N random listings for the gold set and write ONLY their listing_ids into
data/gold/gold_25.csv (every Group B column left blank for hand-labeling).

Random, but SEEDED so the choice is reproducible and defensible ("a random
sample of 25, seed=42") -- change the seed for a different draw.

Only listings that actually have a description (i.e. were fetched) are eligible,
since you can't hand-label a listing with no text to read.

Run:  python pick_gold.py            # 25 rows, seed 42
      python pick_gold.py 25 7       # 25 rows, seed 7
"""

import csv
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "bayut.db"
GOLD = ROOT / "data" / "gold" / "gold_25.csv"


def main(n=25, seed=42):
    conn = sqlite3.connect(DB)
    ids = [r[0] for r in conn.execute(
        "SELECT listing_id FROM records WHERE description_raw IS NOT NULL")]
    if len(ids) < n:
        print(f"only {len(ids)} labelable listings available; using all of them")
        n = len(ids)

    random.seed(seed)
    picked = sorted(random.sample(ids, n))

    # Preserve the existing header (the 22 Group B column names).
    header = None
    had_data = False
    if GOLD.exists():
        with open(GOLD, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        if rows:
            header = rows[0]
            had_data = len(rows) > 1
    if header is None:
        header = ["listing_id", "compound_name", "developer_name", "governorate",
                  "city", "district", "finishing_level", "delivery_status",
                  "delivery_date", "sale_type", "payment_type", "down_payment_amount",
                  "down_payment_pct", "installment_years", "installment_amount",
                  "installment_frequency", "cash_discount_pct", "amenities",
                  "floor_number", "garden_area_sqm", "roof_area_sqm", "is_negotiable"]

    # Don't silently clobber existing labels.
    if had_data:
        bak = GOLD.with_suffix(".csv.bak")
        bak.write_text(GOLD.read_text(encoding="utf-8-sig"), encoding="utf-8")
        print(f"existing labels found -> backed up to {bak.name}")

    with open(GOLD, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for lid in picked:
            w.writerow([lid] + [""] * (len(header) - 1))

    print(f"wrote {len(picked)} listing_ids to {GOLD}  (seed={seed})\n")
    print("open these and hand-label (blank = null):")
    for lid in picked:
        print(f"  {lid}  https://www.bayut.eg/تفاصيل-{lid}/العقار.html")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    main(n, seed)
