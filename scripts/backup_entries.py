#!/usr/bin/env python3
"""
backup_entries.py

Full-fidelity backup of the `entries` table -- unlike export_translations.py
(which deliberately collapses everything into the game's {ja: value} shape
for a single chosen language), this keeps file/entry_id/ja/en/es/status as
separate columns, so it can be safely diffed, audited, or restored from
without losing which language is which.

This is the file to keep as your real backup / source of truth snapshot --
never reseed the DB from a game-format export.

Usage:
    export DATABASE_URL="postgresql://...supabase connection string..."
    python backup_entries.py --output backup_2026-08-05.csv
"""

import argparse
import csv
import os
import sys
from datetime import date

try:
    import psycopg2
except ImportError:
    print(
        "[!] psycopg2 is not installed. Run:\n"
        "    pip install psycopg2-binary --break-system-packages",
        file=sys.stderr,
    )
    sys.exit(1)

COLUMNS = ["file", "entry_id", "ja", "en", "es", "status", "updated_by", "updated_at"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_name = f"backup_{date.today().isoformat()}.csv"
    parser.add_argument("--output", "-o", default=default_name, help=f"Output CSV path (default: {default_name})")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("[!] DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    total = 0
    with conn.cursor() as cur, open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)

        cur.execute(f"SELECT {', '.join(COLUMNS)} FROM entries ORDER BY file, entry_id")
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            writer.writerows(batch)
            total += len(batch)

    conn.close()
    print(f"Backup complete: {total} rows written to {args.output}")


if __name__ == "__main__":
    main()
