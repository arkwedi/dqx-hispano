#!/usr/bin/env python3
"""
migrate_to_supabase.py

Pushes every row from the local translations.db (built earlier with
build_translation_db.py) into the `entries` table on Supabase.

Requires the DATABASE_URL environment variable to be set to your Supabase
Postgres connection string (Project Settings -> Database -> Connection
string -> URI, with [YOUR-PASSWORD] replaced by your real password).

Usage:
    export DATABASE_URL="postgresql://postgres:yourpassword@db.xxxx.supabase.co:5432/postgres"
    python migrate_to_supabase.py translations.db

Safe to re-run: existing (file, entry_id) rows are left untouched
(ON CONFLICT DO NOTHING), so this will never overwrite translations that
collaborators have already entered in Supabase.
"""

import argparse
import os
import sqlite3
import sys

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print(
        "[!] psycopg2 is not installed. Run:\n"
        "    pip install psycopg2-binary --break-system-packages\n"
        "(or just `pip install psycopg2-binary` outside this sandboxed env)",
        file=sys.stderr,
    )
    sys.exit(1)

BATCH_SIZE = 2000

INSERT_SQL = """
    INSERT INTO entries (file, entry_id, ja, en)
    VALUES %s
    ON CONFLICT (file, entry_id) DO NOTHING
"""


def read_sqlite_rows(sqlite_path: str):
    conn = sqlite3.connect(sqlite_path)
    cur = conn.execute("SELECT file, entry_id, ja, en FROM entries ORDER BY id")
    while True:
        rows = cur.fetchmany(BATCH_SIZE)
        if not rows:
            break
        yield rows
    conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_db", help="Path to the local translations.db")
    args = parser.parse_args()

    if not os.path.exists(args.sqlite_db):
        print(f"[!] {args.sqlite_db} not found", file=sys.stderr)
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print(
            "[!] DATABASE_URL environment variable is not set. See the "
            "docstring at the top of this script for how to get it from "
            "the Supabase dashboard.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Connecting to Supabase...")
    pg_conn = psycopg2.connect(db_url)
    pg_conn.autocommit = False

    total_sent = 0
    try:
        with pg_conn.cursor() as cur:
            for batch in read_sqlite_rows(args.sqlite_db):
                execute_values(cur, INSERT_SQL, batch)
                pg_conn.commit()
                total_sent += len(batch)
                print(f"  ...{total_sent} rows sent")
    except Exception:
        pg_conn.rollback()
        raise
    finally:
        pg_conn.close()

    print(f"\nDone. {total_sent} rows processed (existing rows were skipped, not overwritten).")


if __name__ == "__main__":
    main()
