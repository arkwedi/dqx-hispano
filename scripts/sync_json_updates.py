#!/usr/bin/env python3
"""
sync_json_updates.py

Run this after build_translation_db.py has produced a fresh local
translations.db from your updated JSON folder (DAT -> raw JSON -> latest EN
from Clarity merged in). This compares that local snapshot against Supabase
and brings Supabase up to date, without disturbing existing ES work.

JA comes straight from the game's DAT files, so it almost never changes
outside of an actual content update. EN gets edited by the Clarity team
routinely (typo fixes, wording tweaks) as part of their daily refresh. The
two are treated differently on purpose:

- Genuinely new (file, entry_id) pairs are inserted as new rows
  (es='', status='pendiente').
- Existing entries where ONLY en changed (ja is the same) are auto-updated
  in place -- your ES translation and status are left untouched, this just
  keeps the EN reference column current so translators aren't looking at a
  stale typo. These are logged, not flagged for review, since it's routine.
- Existing entries where ja changed (rare -- means the underlying game
  content actually changed) are NOT auto-applied. They're written to a
  review CSV, because this is the case that can genuinely invalidate an
  existing ES translation.
- Unchanged entries are skipped entirely.

PERFORMANCE NOTE: earlier versions of this script ran one SELECT per local
row against Supabase -- with 269k rows, that's 269k network round trips,
which is what made it look "stuck" for 30+ minutes (and since it only
committed once at the very end, nothing was saved if it didn't finish).
This version fetches the entire current Supabase state ONCE into memory,
does the diffing locally in Python, and applies changes in batches. Should
finish in well under a minute even at this scale.

Usage:
    export DATABASE_URL="postgresql://...supabase connection string..."
    python sync_json_updates.py translations.db
    python sync_json_updates.py translations.db --review-output changed_2026-08-05.csv
"""

import argparse
import csv
import os
import sqlite3
import sys

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print(
        "[!] psycopg2 is not installed. Run:\n"
        "    pip install psycopg2-binary --break-system-packages",
        file=sys.stderr,
    )
    sys.exit(1)

BATCH_SIZE = 2000


def read_local_entries(sqlite_path):
    """Yield (file, entry_id, ja, en) rows from the local translations.db
    built by build_translation_db.py."""
    conn = sqlite3.connect(sqlite_path)
    cur = conn.execute("SELECT file, entry_id, ja, en FROM entries ORDER BY file, entry_id")
    while True:
        batch = cur.fetchmany(5000)
        if not batch:
            break
        for row in batch:
            yield row
    conn.close()


def fetch_supabase_state(conn):
    """One query, not one-per-row: pulls the whole current (file, entry_id)
    -> (ja, en) state from Supabase into a local dict.

    Uses a plain client-side cursor with fetchmany() paging, NOT a named
    (server-side) cursor -- those require holding state on a single
    physical connection, which breaks under PgBouncer's transaction-mode
    pooling (the connection string you're using on port 6543 to work
    around the IPv6 issue)."""
    state = {}
    with conn.cursor() as cur:
        cur.execute("SELECT file, entry_id, ja, en FROM entries")
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            for file_key, entry_id, ja, en in batch:
                state[(file_key, entry_id)] = (ja, en)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sqlite_db", help="Path to the local translations.db built by build_translation_db.py")
    parser.add_argument("--review-output", default="changed_entries.csv",
                         help="CSV listing existing entries whose JA text changed (default: changed_entries.csv)")
    args = parser.parse_args()

    if not os.path.exists(args.sqlite_db):
        print(f"[!] {args.sqlite_db} not found. Run build_translation_db.py first.", file=sys.stderr)
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("[!] DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    print("Descargando el estado actual de Supabase (una sola consulta)...")
    supabase_state = fetch_supabase_state(conn)
    print(f"  {len(supabase_state)} filas existentes en Supabase")

    print("Comparando contra el snapshot local...")
    new_rows = []
    en_updates = []         # (en, file, entry_id) -- applied in batch
    ja_changed_rows = []    # written to the review CSV, never auto-applied
    unchanged_count = 0

    for file_key, entry_id, ja, en in read_local_entries(args.sqlite_db):
        existing = supabase_state.get((file_key, entry_id))

        if existing is None:
            new_rows.append((file_key, entry_id, ja, en))
            continue

        old_ja, old_en = existing
        if old_ja != ja:
            # Rare, meaningful case: game content itself changed.
            # Never auto-apply -- surfaced for review.
            ja_changed_rows.append({
                "file": file_key, "entry_id": entry_id,
                "ja_old": old_ja, "ja_new": ja,
                "en_old": old_en, "en_new": en,
            })
        elif old_en != en:
            # Routine: Clarity's daily EN refresh. JA unchanged, so ES
            # translation is still valid -- just refresh the EN reference.
            en_updates.append((en, file_key, entry_id))
        else:
            unchanged_count += 1

    print(f"  {len(new_rows)} nuevas, {len(en_updates)} con EN actualizado, "
          f"{len(ja_changed_rows)} con JA modificado, {unchanged_count} sin cambios")

    try:
        with conn.cursor() as cur:
            if new_rows:
                print(f"Insertando {len(new_rows)} filas nuevas...")
                for i in range(0, len(new_rows), BATCH_SIZE):
                    execute_values(
                        cur,
                        "INSERT INTO entries (file, entry_id, ja, en) VALUES %s "
                        "ON CONFLICT (file, entry_id) DO NOTHING",
                        new_rows[i:i + BATCH_SIZE],
                    )

            if en_updates:
                print(f"Actualizando EN en {len(en_updates)} filas existentes...")
                for i in range(0, len(en_updates), BATCH_SIZE):
                    execute_values(
                        cur,
                        "UPDATE entries AS e SET en = v.en "
                        "FROM (VALUES %s) AS v(en, file, entry_id) "
                        "WHERE e.file = v.file AND e.entry_id = v.entry_id",
                        en_updates[i:i + BATCH_SIZE],
                    )
        conn.commit()
        print("Cambios confirmados en Supabase.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if ja_changed_rows:
        with open(args.review_output, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["file", "entry_id", "ja_old", "ja_new", "en_old", "en_new"])
            writer.writeheader()
            writer.writerows(ja_changed_rows)

    print(f"\nNuevas entradas insertadas: {len(new_rows)}")
    print(f"Referencias EN actualizadas automaticamente (JA sin cambios): {len(en_updates)}")
    print(f"Entradas sin cambios: {unchanged_count}")
    print(f"Entradas con JA modificado (posible cambio real de contenido): {len(ja_changed_rows)}"
          + (f" -- detalle en {args.review_output}" if ja_changed_rows else ""))
    if ja_changed_rows:
        print("Estas NO se tocaron en Supabase -- su ES y estado actual siguen intactos. "
              "Revisa el CSV y decide manualmente si ameritan re-traduccion.")


if __name__ == "__main__":
    main()
