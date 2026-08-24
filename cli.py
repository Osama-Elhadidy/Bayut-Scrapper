"""
Single entrypoint, one subcommand per pipeline stage.

    python cli.py discover [--limit N]
    python cli.py session              # one-time: clear a bot challenge by hand
    python cli.py fetch [--limit N] [--headless]
    python cli.py parse [--limit N]
    python cli.py extract [--limit N] [--model NAME]
    python cli.py backfill [--limit N]   # re-apply structural fields, no LLM
    python cli.py evaluate
    python cli.py export
    python cli.py run [--limit N]      # fetch -> parse -> extract -> export
    python cli.py status
    python cli.py failures

Every stage is idempotent and resumable: it reads whatever is pending from
SQLite and stops when that's empty. Running the same command twice in a row
does nothing the second time -- that's the whole idempotency story, and the
`status` command is how you watch it happen.
"""

import argparse
import asyncio
import json
import sys

# Windows' default console codepage (cp1252) can't print Arabic text, and
# this pipeline's data is full of it -- governorate names, failure
# messages, listing titles. Without this, a `status` or `failures` call
# crashes mid-print the first time it hits a non-Latin character.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import (backfill, config, db, discover, evaluate, export, extract,
                 fetch, parse)


def cmd_discover(args):
    conn = db.connect()
    discover.discover(conn, per_slice=args.limit or 200)
    cmd_status(args)


def cmd_session(args):
    asyncio.run(fetch.establish_session())


def cmd_fetch(args):
    conn = db.connect()
    asyncio.run(fetch.fetch_pages(conn, limit=args.limit, headless=args.headless))
    cmd_status(args)


def cmd_parse(args):
    conn = db.connect()
    parse.parse_pending(conn, limit=args.limit)


def cmd_extract(args):
    conn = db.connect()
    extract.extract_pending(conn, limit=args.limit, model=args.model)


def cmd_backfill(args):
    conn = db.connect()
    backfill.backfill(conn, limit=args.limit)


def cmd_evaluate(args):
    conn = db.connect()
    results = evaluate.evaluate(conn, gold_path=args.gold)
    print(evaluate.to_markdown(results))


def cmd_export(args):
    conn = db.connect()
    export.export(conn, gold_path=args.gold)


def cmd_run(args):
    conn = db.connect()
    print("== fetch =="); asyncio.run(fetch.fetch_pages(conn, limit=args.limit))
    print("== parse =="); parse.parse_pending(conn, limit=args.limit)
    print("== extract =="); extract.extract_pending(conn, limit=args.limit)
    print("== backfill =="); backfill.backfill(conn, limit=args.limit)
    print("== export =="); export.export(conn)


def cmd_status(args):
    conn = db.connect()

    def one(sql, *p):
        return conn.execute(sql, p).fetchone()[0]

    listings = one("SELECT COUNT(*) FROM listings")
    pages = one("SELECT COUNT(*) FROM pages")
    records = one("SELECT COUNT(*) FROM records")
    with_desc = one("SELECT COUNT(*) FROM records WHERE description_raw IS NOT NULL")
    extracted = one("SELECT COUNT(DISTINCT listing_id) FROM extractions WHERE error IS NULL")
    fails = one("SELECT COUNT(*) FROM failures")

    print(f"listings   {listings}")
    print(f"pages      {pages}  (pending fetch: {len(db.pending_fetch(conn))})")
    print(f"records    {records}  (pending parse: {len(db.pending_parse(conn))})")
    print(f"  with description_raw: {with_desc}")
    print(f"extracted  {extracted}  (pending extract: {len(db.pending_extract(conn))})")
    print(f"failures   {fails}")

    cost_row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(tokens_in),0), "
        "COALESCE(SUM(tokens_out),0) FROM extractions").fetchone()
    print(f"extraction cost so far: ${cost_row[0]:.4f}  "
          f"(tokens in={cost_row[1]:.0f} out={cost_row[2]:.0f})")


def cmd_failures(args):
    conn = db.connect()
    rows = conn.execute(
        "SELECT stage, error_class, substr(message,1,80) m, COUNT(*) n "
        "FROM failures GROUP BY stage, error_class, m ORDER BY n DESC LIMIT 30").fetchall()
    if not rows:
        print("none")
    for r in rows:
        print(f"  {r['n']:5d}  {r['stage']:8s} {r['error_class'] or '':10s}  {r['m']}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    for name, fn in [("discover", cmd_discover), ("fetch", cmd_fetch),
                      ("parse", cmd_parse), ("extract", cmd_extract),
                      ("backfill", cmd_backfill), ("run", cmd_run)]:
        sp = sub.add_parser(name)
        sp.add_argument("--limit", type=int, default=None)
        if name == "fetch":
            sp.add_argument("--headless", action="store_true")
        if name == "extract":
            sp.add_argument("--model", default=None)
        sp.set_defaults(func=fn)

    sp = sub.add_parser("session"); sp.set_defaults(func=cmd_session)

    for name, fn in [("evaluate", cmd_evaluate), ("export", cmd_export)]:
        sp = sub.add_parser(name)
        sp.add_argument("--gold", default=None)
        sp.set_defaults(func=fn)

    sp = sub.add_parser("status"); sp.set_defaults(func=cmd_status)
    sp = sub.add_parser("failures"); sp.set_defaults(func=cmd_failures)

    args = p.parse_args()
    if not getattr(args, "cmd", None):
        p.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
