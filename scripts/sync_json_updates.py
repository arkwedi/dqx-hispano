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

RE-KEYING (new): in a big game update the numeric entry_id of a line can
change while its text stays the same (e.g. a new monster is inserted in
subPackage02Client and everything after it shifts). If we only looked at
(file, entry_id), the old row -- with its ES translation -- would be left
stranded at the old id, a fresh empty row would be inserted at the new id,
and the exported JSON would put the old text into a slot the game now uses
for something else.

So before the comparison above, this script looks, file by file, for rows
in Supabase whose (file, entry_id) is no longer valid but whose JA text
(and EN, when possible) still exists in the new data at a different
entry_id of the SAME file, and moves the row -- id, ES, status, history
and all -- to its new entry_id:

  1. First pass matches on (ja, en). Second pass matches on ja alone (EN
     may have been refreshed in the same update). --strict disables it.
  2. Only rows that carry a translation (es not empty) are moved. They can
     replace an empty row that a previous sync already inserted at the new
     id (this repairs the damage from a run that happened before this fix).
  3. When the destination id is occupied by another row that isn't moving:
     an EMPTY row is deleted (nothing is lost, sync regenerates it), a row
     WITH ES is parked under entry_id '__stale__<id>' instead of deleted,
     so no translation is ever lost. export_translations.py skips parked
     rows. Parked rows can be revived automatically if their text comes
     back, or removed by hand whenever you decide.
  4. Duplicated JA texts inside a file are paired in numeric entry_id order;
     when the counts don't line up the pair is tagged "(ambiguo)" in the log.

Every move/park/delete is written to a CSV so it can be audited, and
--dry-run reports everything without touching Supabase.

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
    python sync_json_updates.py translations.db --dry-run
    python sync_json_updates.py translations.db --review-output changed_2026-08-05.csv
"""

import argparse
import csv
import datetime
import os
import sqlite3
import sys
from collections import defaultdict

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

# Prefix for rows that lost their slot and couldn't be re-homed. Keep this in
# sync with PARKED_PREFIX in export_translations.py (which skips them).
PARKED_PREFIX = "__stale__"
# Temporary entry_ids used mid-transaction so swaps/cycles never collide on
# the UNIQUE(file, entry_id) constraint. Never visible after commit.
TEMP_PREFIX = "__mv__"


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
    -> (ja, en, es) state from Supabase into a local dict.

    Uses a plain client-side cursor with fetchmany() paging, NOT a named
    (server-side) cursor -- those require holding state on a single
    physical connection, which breaks under PgBouncer's transaction-mode
    pooling (the connection string you're using on port 6543 to work
    around the IPv6 issue)."""
    state = {}
    with conn.cursor() as cur:
        cur.execute("SELECT file, entry_id, ja, en, es FROM entries")
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            for file_key, entry_id, ja, en, es in batch:
                state[(file_key, entry_id)] = (ja, en, es or "")
    return state


# ---------------------------------------------------------------------------
# Re-keying: detect rows whose text moved to another entry_id in the same file
# ---------------------------------------------------------------------------

def _id_sort_key(entry_id):
    """Numeric ids sort numerically ('99' < '100'); anything else after them."""
    return (0, int(entry_id), "") if entry_id.isdigit() else (1, 0, entry_id)


def _has_es(row):
    """row = (ja, en, es)"""
    return bool(row[2] and row[2].strip())


def plan_rekeys(local_rows, supabase_state, strict=False):
    """Pure function (no DB access). Decides what to do with rows whose
    entry_id changed. Returns a dict:

      moves   : [(file, old_id, new_id, kind)]  re-key a row that has ES
      parked  : [(file, old_id, park_id)]       displaced row WITH ES, renamed away
      deleted : [(file, entry_id)]              displaced row WITHOUT ES, removed

    Terminology, per file:
      placed    = Supabase rows whose (file, entry_id) exists locally with the
                  same ja -> they're where they belong, leave them alone.
      stale     = every other Supabase row (old id, ja changed, parked...).
                  Only the ones with ES are worth rescuing ("candidates").
      slots     = local entries that need a row: id missing in Supabase, id
                  taken by a row with a different ja, or id already holding
                  an EMPTY row (e.g. inserted by a previous sync).
    """
    local_by_file = defaultdict(dict)
    for file_key, entry_id, ja, en in local_rows:
        local_by_file[file_key][entry_id] = (ja, en)

    supa_by_file = defaultdict(dict)
    for (file_key, entry_id), row in supabase_state.items():
        supa_by_file[file_key][entry_id] = row

    moves = []

    for file_key, local in local_by_file.items():
        supa = supa_by_file.get(file_key)
        if not supa:
            continue

        placed = {i for i, row in supa.items() if i in local and local[i][0] == row[0]}
        candidates = {i: row for i, row in supa.items() if i not in placed and _has_es(row)}
        if not candidates:
            continue

        slots = {}
        for i, (ja, en) in local.items():
            if i not in supa or i not in placed or not _has_es(supa[i]):
                slots[i] = (ja, en)
        if not slots:
            continue

        used_slots, used_cands = set(), set()
        passes = [("ja+en", lambda ja, en: (ja, en))]
        if not strict:
            passes.append(("ja", lambda ja, en: ja))

        for kind, keyf in passes:
            slot_groups, cand_groups = defaultdict(list), defaultdict(list)
            for i, (ja, en) in slots.items():
                if i not in used_slots:
                    slot_groups[keyf(ja, en)].append(i)
            for i, (ja, en, _es) in candidates.items():
                if i not in used_cands:
                    cand_groups[keyf(ja, en)].append(i)

            for group_key, slot_ids in slot_groups.items():
                cand_ids = cand_groups.get(group_key)
                if not cand_ids:
                    continue
                slot_ids.sort(key=_id_sort_key)
                cand_ids.sort(key=_id_sort_key)
                label = kind if len(slot_ids) == len(cand_ids) else f"{kind} (ambiguo)"
                for slot_id, cand_id in zip(slot_ids, cand_ids):
                    moves.append((file_key, cand_id, slot_id, label))
                    used_slots.add(slot_id)
                    used_cands.add(cand_id)

    # Whoever currently sits in a destination slot (and isn't moving away
    # itself) has to be displaced: deleted if empty, parked if translated.
    moving_from = {(f, old) for f, old, _new, _k in moves}
    taken = set(supabase_state.keys())
    parked, deleted = [], []
    for file_key, _old, new, _kind in moves:
        occupant_key = (file_key, new)
        if occupant_key not in supabase_state or occupant_key in moving_from:
            continue
        moving_from.add(occupant_key)
        if _has_es(supabase_state[occupant_key]):
            n = 0
            park_id = f"{PARKED_PREFIX}{new}"
            while (file_key, park_id) in taken:
                n += 1
                park_id = f"{PARKED_PREFIX}{new}_{n}"
            taken.add((file_key, park_id))
            parked.append((file_key, new, park_id))
        else:
            deleted.append((file_key, new))

    return {"moves": moves, "parked": parked, "deleted": deleted}


def apply_plan_to_state(state, plan):
    """Mirror the plan on the in-memory Supabase snapshot, so the normal
    new/EN-update/JA-changed comparison that follows sees the post-move world."""
    for key in plan["deleted"]:
        state.pop(key)

    relocations = []
    for file_key, old, new, _kind in plan["moves"]:
        relocations.append(((file_key, new), state.pop((file_key, old))))
    for file_key, old, park_id in plan["parked"]:
        relocations.append(((file_key, park_id), state.pop((file_key, old))))

    for key, row in relocations:
        if key in state:
            raise RuntimeError(f"Colision inesperada al reubicar filas: {key}")
        state[key] = row


def plan_to_report_rows(plan, supabase_state_before):
    """Flat rows for the audit CSV. Must be called BEFORE apply_plan_to_state."""
    rows = []
    for file_key, old, new, kind in plan["moves"]:
        ja, en, es = supabase_state_before[(file_key, old)]
        rows.append({"accion": "mover", "file": file_key, "entry_id_viejo": old,
                     "entry_id_nuevo": new, "ja": ja, "en": en, "es": es, "detalle": kind})
    for file_key, old, park_id in plan["parked"]:
        ja, en, es = supabase_state_before[(file_key, old)]
        rows.append({"accion": "aparcar", "file": file_key, "entry_id_viejo": old,
                     "entry_id_nuevo": park_id, "ja": ja, "en": en, "es": es,
                     "detalle": "ocupaba un id que necesita otra fila; tiene ES, no se borra"})
    for file_key, entry_id in plan["deleted"]:
        ja, en, es = supabase_state_before[(file_key, entry_id)]
        rows.append({"accion": "borrar", "file": file_key, "entry_id_viejo": entry_id,
                     "entry_id_nuevo": "", "ja": ja, "en": en, "es": es,
                     "detalle": "fila sin ES que ocupaba un id necesario"})
    return rows


def _run_batches(cur, sql, rows, expect_rowcount=True):
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        # page_size=BATCH_SIZE keeps each batch a single statement, so
        # cur.rowcount is the real number of rows touched.
        execute_values(cur, sql, batch, page_size=BATCH_SIZE)
        if expect_rowcount and cur.rowcount != len(batch):
            raise RuntimeError(
                f"Se esperaba tocar {len(batch)} filas y se tocaron {cur.rowcount}; "
                f"se cancela todo (rollback)."
            )


def apply_plan_to_db(cur, plan):
    """Runs inside the caller's transaction. Order matters:
      0. delete displaced empty rows (frees their ids)
      A. every moving/parked row goes to a unique temporary or park id
         (this vacates all old ids, so cycles and swaps can't collide
         with the UNIQUE(file, entry_id) constraint)
      B. moving rows go from their temporary id to their final id
    """
    if plan["deleted"]:
        _run_batches(
            cur,
            "DELETE FROM entries AS e USING (VALUES %s) AS v(file, entry_id) "
            "WHERE e.file = v.file AND e.entry_id = v.entry_id",
            plan["deleted"],
        )

    move_rows = plan["moves"]
    phase_a = [(f, old, f"{TEMP_PREFIX}{n}") for n, (f, old, _new, _k) in enumerate(move_rows)]
    phase_a += [(f, old, park_id) for f, old, park_id in plan["parked"]]
    phase_b = [(f, f"{TEMP_PREFIX}{n}", new) for n, (f, _old, new, _k) in enumerate(move_rows)]

    rename_sql = (
        "UPDATE entries AS e SET entry_id = v.new_id "
        "FROM (VALUES %s) AS v(file, old_id, new_id) "
        "WHERE e.file = v.file AND e.entry_id = v.old_id"
    )
    if phase_a:
        _run_batches(cur, rename_sql, phase_a)
    if phase_b:
        _run_batches(cur, rename_sql, phase_b)


def write_rekey_report(path, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["accion", "file", "entry_id_viejo", "entry_id_nuevo", "ja", "en", "es", "detalle"])
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sqlite_db", help="Path to the local translations.db built by build_translation_db.py")
    parser.add_argument("--review-output", default="changed_entries.csv",
                         help="CSV listing existing entries whose JA text changed (default: changed_entries.csv)")
    parser.add_argument("--moves-output", default=None,
                         help="CSV auditing every re-keyed/parked/deleted row. Default: moved_entries_<today>.csv "
                              "next to --review-output (so the workflow uploads it with the other logs)")
    parser.add_argument("--strict", action="store_true",
                         help="Re-key only when BOTH ja and en match (default also accepts ja alone, in case "
                              "EN was refreshed in the same update)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Report everything and write the CSVs, but do NOT modify Supabase")
    args = parser.parse_args()

    if not os.path.exists(args.sqlite_db):
        print(f"[!] {args.sqlite_db} not found. Run build_translation_db.py first.", file=sys.stderr)
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("[!] DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    if args.moves_output is None:
        folder = os.path.dirname(args.review_output)
        args.moves_output = os.path.join(folder, f"moved_entries_{datetime.date.today().isoformat()}.csv")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    print("Descargando el estado actual de Supabase (una sola consulta)...")
    supabase_state = fetch_supabase_state(conn)
    print(f"  {len(supabase_state)} filas existentes en Supabase")

    local_rows = list(read_local_entries(args.sqlite_db))

    print("Buscando filas cuyo texto cambio de entry_id dentro del mismo archivo...")
    plan = plan_rekeys(local_rows, supabase_state, strict=args.strict)
    rekey_report = plan_to_report_rows(plan, supabase_state)
    n_moves, n_parked, n_deleted = len(plan["moves"]), len(plan["parked"]), len(plan["deleted"])
    n_ambiguous = sum(1 for m in plan["moves"] if "ambiguo" in m[3])
    print(f"  {n_moves} filas a reubicar ({n_ambiguous} ambiguas), "
          f"{n_parked} desplazadas con ES aparcadas, {n_deleted} desplazadas vacias borradas")
    if rekey_report:
        write_rekey_report(args.moves_output, rekey_report)
        print(f"  detalle en {args.moves_output}")
    apply_plan_to_state(supabase_state, plan)

    print("Comparando contra el snapshot local...")
    new_rows = []
    en_updates = []         # (en, file, entry_id) -- applied in batch
    ja_changed_rows = []    # written to the review CSV, never auto-applied
    unchanged_count = 0

    for file_key, entry_id, ja, en in local_rows:
        existing = supabase_state.get((file_key, entry_id))

        if existing is None:
            new_rows.append((file_key, entry_id, ja, en))
            continue

        old_ja, old_en, _old_es = existing
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

    if args.dry_run:
        conn.rollback()
        conn.close()
        print("\n[dry-run] No se modifico nada en Supabase.")
    else:
        try:
            with conn.cursor() as cur:
                if rekey_report:
                    print(f"Reubicando filas ({n_moves} movidas, {n_parked} aparcadas, {n_deleted} borradas)...")
                    apply_plan_to_db(cur, plan)

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
        os.makedirs(os.path.dirname(os.path.abspath(args.review_output)), exist_ok=True)
        with open(args.review_output, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["file", "entry_id", "ja_old", "ja_new", "en_old", "en_new"])
            writer.writeheader()
            writer.writerows(ja_changed_rows)

    print(f"\nFilas reubicadas a su nuevo entry_id (con su ES): {n_moves}")
    if n_parked or n_deleted:
        print(f"  Desplazadas: {n_parked} con ES aparcadas como '{PARKED_PREFIX}*', {n_deleted} vacias borradas")
    print(f"Nuevas entradas insertadas: {len(new_rows)}")
    print(f"Referencias EN actualizadas automaticamente (JA sin cambios): {len(en_updates)}")
    print(f"Entradas sin cambios: {unchanged_count}")
    print(f"Entradas con JA modificado (posible cambio real de contenido): {len(ja_changed_rows)}"
          + (f" -- detalle en {args.review_output}" if ja_changed_rows else ""))
    if ja_changed_rows:
        print("Estas NO se tocaron en Supabase -- su ES y estado actual siguen intactos. "
              "Revisa el CSV y decide manualmente si ameritan re-traduccion.")


if __name__ == "__main__":
    main()
