#!/usr/bin/env python3
"""
glossary_bulk_update.py

For master-list files (weapon/spell/monster names, etc.) where each JSON
entry is essentially {"ja_term": "en_term"} rather than a full dialogue
line, this lets you upload a glossary (CSV or JSON) with the JA term and
its agreed ES equivalent, and apply it across every matching row in
Supabase in one go -- instead of retyping the same term by hand every time
it shows up.

Match strategy: EXACT match against the `ja` column (row.ja == term), which
is what glossary/name-list files look like. This intentionally does NOT do
substring replacement inside long dialogue lines that merely mention the
term -- that's a different, riskier operation already covered by the
"Buscar y reemplazar" panel in the frontend (one term at a time, with a
preview before applying).

Glossary file format (CSV), default column names -- extra columns (e.g. an
English one) are simply ignored:
    ja,es
    はやぶさの剣,Espada Halcon
    ホイミスライム,Limo Curasano

Or JSON:
    [{"ja": "はやぶさの剣", "es": "Espada Halcon"}, ...]

If your file uses different column/key names, point at them with
--ja-col / --es-col instead of renaming anything in your file, e.g.:
    python glossary_bulk_update.py monstruos.csv --ja-col japones --es-col espanol

Usage:
    export DATABASE_URL="postgresql://...supabase connection string..."

    # Preview only, no writes:
    python glossary_bulk_update.py glosario.csv --dry-run

    # Apply, but only to rows where ES is still empty (safe default):
    python glossary_bulk_update.py glosario.csv

    # Apply and overwrite ES even where a translation already exists:
    python glossary_bulk_update.py glosario.csv --force

    # Your file uses different column names:
    python glossary_bulk_update.py monstruos.csv --ja-col JP --es-col ES
"""

import argparse
import csv
import json
import os
import sys


try:
    import psycopg2
except ImportError:
    print(
        "[!] psycopg2 is not installed. Run:\n"
        "    pip install psycopg2-binary --break-system-packages",
        file=sys.stderr,
    )
    sys.exit(1)


def detect_delimiter(sample_line):
    # Excel in Spanish-locale regions commonly exports CSV with ';' instead
    # of ',' -- detect whichever separator is actually being used.
    if sample_line.count(";") > sample_line.count(","):
        return ";"
    return ","


def load_glossary(path, ja_col="ja", es_col="es"):
    if path.lower().endswith(".json"):
        data = json.loads(open(path, encoding="utf-8").read())
        try:
            pairs = [(row[ja_col], row[es_col]) for row in data]
        except KeyError as e:
            print(f"[!] Column {e} not found in the JSON objects. "
                  f"Available keys in the first row: {list(data[0].keys())}", file=sys.stderr)
            sys.exit(1)
    else:
        with open(path, encoding="utf-8-sig", newline="") as f:
            first_line = f.readline()
            f.seek(0)
            delimiter = detect_delimiter(first_line)
            reader = csv.DictReader(f, delimiter=delimiter)
            if ja_col not in reader.fieldnames or es_col not in reader.fieldnames:
                print(f"[!] Columns '{ja_col}'/'{es_col}' not found. "
                      f"This file has: {reader.fieldnames}. "
                      f"Use --ja-col / --es-col to point at the right ones.", file=sys.stderr)
                sys.exit(1)
            pairs = [(row[ja_col], row[es_col]) for row in reader]

    pairs = [(ja.strip(), es.strip()) for ja, es in pairs if ja.strip() and es.strip()]
    if not pairs:
        print("[!] No valid ja/es pairs found in the glossary file.", file=sys.stderr)
        sys.exit(1)
    return pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("glossary_file", help="CSV or JSON file with ja/es columns")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")
    parser.add_argument("--force", action="store_true", help="Overwrite ES even if already filled in")
    parser.add_argument("--ja-col", default="ja", help="Column/key name for the Japanese term (default: ja)")
    parser.add_argument("--es-col", default="es", help="Column/key name for the Spanish term (default: es)")
    args = parser.parse_args()

    if not os.path.exists(args.glossary_file):
        print(f"[!] {args.glossary_file} not found", file=sys.stderr)
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("[!] DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    pairs = load_glossary(args.glossary_file, args.ja_col, args.es_col)
    print(f"Loaded {len(pairs)} glossary terms from {args.glossary_file}")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    total_matched = 0
    total_updated = 0

    try:
        with conn.cursor() as cur:
            for ja_term, es_term in pairs:
                if args.force:
                    cur.execute("SELECT file, entry_id, es FROM entries WHERE ja = %s", (ja_term,))
                else:
                    cur.execute(
                        "SELECT file, entry_id, es FROM entries WHERE ja = %s AND (es = '' OR es IS NULL)",
                        (ja_term,),
                    )
                matches = cur.fetchall()
                total_matched += len(matches)

                if not matches:
                    continue

                print(f"\n'{ja_term}' -> '{es_term}': {len(matches)} fila(s)")
                for file_key, entry_id, old_es in matches:
                    tag = "(sobrescribe)" if old_es else "(nuevo)"
                    print(f"  {tag} {file_key} #{entry_id}: '{old_es}' -> '{es_term}'")

                if not args.dry_run:
                    cur.execute(
                        "UPDATE entries SET es = %s, updated_by = %s, updated_at = now() WHERE ja = %s",
                        (es_term, "glossary_bulk_update", ja_term),
                    ) if args.force else cur.execute(
                        "UPDATE entries SET es = %s, updated_by = %s, updated_at = now() "
                        "WHERE ja = %s AND (es = '' OR es IS NULL)",
                        (es_term, "glossary_bulk_update", ja_term),
                    )
                    total_updated += cur.rowcount

        if args.dry_run:
            conn.rollback()
            print(f"\n[dry-run] {total_matched} filas coinciden en total. Nada se escribio.")
        else:
            conn.commit()
            print(f"\nListo: {total_updated} filas actualizadas ({total_matched} coincidencias encontradas).")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
