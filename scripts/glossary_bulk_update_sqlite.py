#!/usr/bin/env python3
"""
glossary_bulk_update_sqlite.py

Same idea as glossary_bulk_update.py, but targets Clarity's LOCAL glossary
SQLite database instead of your Supabase entries table -- for overwriting
the 'en' column (the text DeepL/Clarity actually displays) with your
Spanish equivalent, so dynamic overworld/battle text picks up the right
term instead of the English one.

IMPORTANT: Clarity re-downloads and wipes these tables on every startup
(see download_custom_files()). Comment out that call while you test, per
what the Clarity dev told you -- otherwise your edits get overwritten the
next time you launch the game.

This DB has separate tables per category, not one universal table:
    m00_strings   -- battle dialogue / boss lines (glossary context for DeepL)
    monsters, npcs, items, key_items, quests, story_names -- name lookups

Not sure which table or column names you're working with? Inspect first:
    python glossary_bulk_update_sqlite.py glossary.db --describe monsters
    python glossary_bulk_update_sqlite.py glossary.db --list-tables

Usage:
    # Preview only, no writes:
    python glossary_bulk_update_sqlite.py glossary.db --table monsters \\
        --glossary monstruos.csv --dry-run

    # Apply, overwriting 'en' with your Spanish term (default: only where
    # a term matches exactly, same safe default as the Supabase version --
    # there's no "already translated" check here since this DB doesn't
    # track a separate ES/status column, so re-running always overwrites
    # matching rows):
    python glossary_bulk_update_sqlite.py glossary.db --table monsters \\
        --glossary monstruos.csv

    # Different column names, different delimiter -- auto-detected, but
    # can be forced:
    python glossary_bulk_update_sqlite.py glossary.db --table npcs \\
        --glossary npcs.csv --ja-col japones --target-col en --delimiter ";"
"""

import argparse
import csv
import json
import os
import sqlite3
import sys


def sniff_delimiter(path):
    with open(path, encoding="utf-8-sig") as f:
        first_line = f.readline()
    for candidate in (",", ";", "\t"):
        if candidate in first_line:
            return candidate
    return ","


def load_glossary(path, ja_col, es_col, delimiter=None):
    if path.lower().endswith(".json"):
        data = json.loads(open(path, encoding="utf-8").read())
        try:
            pairs = [(row[ja_col], row[es_col]) for row in data]
        except KeyError as e:
            print(f"[!] Column {e} not found. Available keys: {list(data[0].keys())}", file=sys.stderr)
            sys.exit(1)
    else:
        delim = delimiter or sniff_delimiter(path)
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=delim)
            if ja_col not in reader.fieldnames or es_col not in reader.fieldnames:
                print(f"[!] Columns '{ja_col}'/'{es_col}' not found. "
                      f"This file has: {reader.fieldnames} (delimiter used: '{delim}'). "
                      f"Use --ja-col / --es-col, or --delimiter if that looks wrong.", file=sys.stderr)
                sys.exit(1)
            pairs = [(row[ja_col], row[es_col]) for row in reader]

    pairs = [(ja.strip(), es.strip()) for ja, es in pairs if ja.strip() and es.strip()]
    if not pairs:
        print("[!] No valid ja/es pairs found in the glossary file.", file=sys.stderr)
        sys.exit(1)
    return pairs


def describe_table(conn, table):
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = cur.fetchall()
    if not cols:
        print(f"[!] Table '{table}' not found (or has no columns).", file=sys.stderr)
        sys.exit(1)
    print(f"Columnas en '{table}':")
    for cid, name, coltype, notnull, default, pk in cols:
        print(f"  {name} ({coltype})")
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"Filas: {count}")


def list_tables(conn):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    print("Tablas encontradas:")
    for (name,) in cur.fetchall():
        print(f"  {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sqlite_db", help="Path to Clarity's local glossary SQLite file")
    parser.add_argument("--glossary", help="CSV or JSON file with ja/es term pairs")
    parser.add_argument("--table", help="Target table (e.g. monsters, npcs, items, key_items, quests, story_names, m00_strings)")
    parser.add_argument("--ja-col", default="ja", help="Column holding the Japanese term (default: ja)")
    parser.add_argument("--target-col", default="en", help="Column to overwrite with the Spanish term (default: en)")
    parser.add_argument("--es-col", default="es", help="Column/key in your glossary FILE holding the Spanish term (default: es)")
    parser.add_argument("--delimiter", help="Force a CSV delimiter instead of auto-detecting")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")
    parser.add_argument("--describe", metavar="TABLE", help="Print columns/row count for TABLE and exit")
    parser.add_argument("--list-tables", action="store_true", help="List all tables in the DB and exit")
    args = parser.parse_args()

    if not os.path.exists(args.sqlite_db):
        print(f"[!] {args.sqlite_db} not found", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.sqlite_db)

    if args.list_tables:
        list_tables(conn)
        return

    if args.describe:
        describe_table(conn, args.describe)
        return

    if not args.glossary or not args.table:
        print("[!] --glossary and --table are required (unless using --describe/--list-tables).", file=sys.stderr)
        sys.exit(1)

    pairs = load_glossary(args.glossary, args.ja_col, args.es_col, args.delimiter)
    print(f"Loaded {len(pairs)} glossary terms from {args.glossary}")

    total_matched = 0
    total_updated = 0
    not_found = []

    cur = conn.cursor()
    for ja_term, es_term in pairs:
        cur.execute(f"SELECT rowid, {args.target_col} FROM {args.table} WHERE {args.ja_col} = ?", (ja_term,))
        matches = cur.fetchall()

        if not matches:
            not_found.append(ja_term)
            continue

        total_matched += len(matches)
        for rowid, old_val in matches:
            print(f"  {args.table}#{rowid}: '{old_val}' -> '{es_term}'")

        if not args.dry_run:
            cur.execute(f"UPDATE {args.table} SET {args.target_col} = ? WHERE {args.ja_col} = ?", (es_term, ja_term))
            total_updated += cur.rowcount

    if args.dry_run:
        conn.rollback()
        print(f"\n[dry-run] {total_matched} filas coinciden en total. Nada se escribio.")
    else:
        conn.commit()
        print(f"\nListo: {total_updated} filas actualizadas en '{args.table}' ({total_matched} coincidencias).")

    if not_found:
        print(f"\n{len(not_found)} termino(s) del glosario no se encontraron en '{args.table}' "
              f"(puede que pertenezcan a otra tabla, o que el JA no coincida exactamente):")
        for term in not_found[:15]:
            print(f"  {term}")
        if len(not_found) > 15:
            print(f"  ...y {len(not_found) - 15} mas")

    conn.close()


if __name__ == "__main__":
    main()
