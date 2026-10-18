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
- Existing entries where ja changed (means the underlying game content
  changed) depend on what the old row holds -- see "SLOTS WITH NEW TEXT".
  The ones that need a human (translated + text similar to the old one,
  i.e. a typo or number edit) are written to a review CSV, because that
  is the case that can invalidate an existing ES translation.
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
     an EMPTY row with no entry_history/proposals is deleted (nothing is lost, sync
     regenerates it); a row WITH ES, history or proposals is parked under
     entry_id '__stale__<id>' instead of deleted, so no translation or
     history is ever lost. export_translations.py skips parked
     rows. Parked rows can be revived automatically if their text comes
     back, or removed by hand whenever you decide.
  4. entry_history (and any other table) must reference entries with
     ON UPDATE CASCADE, otherwise Postgres refuses to change an entry_id.
  5. Duplicated JA texts inside a file are paired in numeric entry_id order;
     when the counts don't line up the pair is tagged "(ambiguo)" in the log.

SLOTS WITH NEW TEXT: after the re-keying, an id whose Supabase row still has
a different ja can't be left alone -- export_translations.py would write the
old text into a slot the game now uses for something else (e.g. "Nadraga"
shown where the game now has "Estella"). So:
  - old row without ES and without history -> its ja/en are overwritten in
    place (nothing to lose);
  - old row WITH ES/history and very different text (similarity below
    --similarity, default 0.5) -> parked as '__stale__<id>', a fresh row is
    inserted for the new text;
  - old row WITH ES/history and similar text (typo, a number...) -> left as
    is and listed in the review CSV (with its ES and a similarity score).
Rows are matched by (file, ja[, en]) but never keyed by them: the same JA
line legitimately repeats inside a file (shop dialogue, "yes/no" prompts),
so (file, ja) is not unique -- (file, entry_id) stays the key.

Every move/park/delete/replace is written to a CSV so it can be audited, and
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
    python sync_json_updates.py translations.db --similarity 0.6
    python sync_json_updates.py translations.db --review-output changed_2026-08-05.csv
"""

import argparse
import csv
import datetime
import difflib
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


def fetch_history_keys(conn):
    """(file, entry_id) pairs that carry work beyond the ES text itself:
    at least one entry_history row, or at least one translation_proposals
    row (the review queue points at entries by entries.id). Displaced rows
    in this set get parked instead of deleted/overwritten, so history and
    pending proposals are never lost. Each query runs under a savepoint so
    a missing/different table never aborts the transaction."""
    queries = [
        ("entry_history", "SELECT DISTINCT file, entry_id FROM entry_history"),
        ("translation_proposals",
         "SELECT DISTINCT e.file, e.entry_id FROM translation_proposals p "
         "JOIN entries e ON e.id = p.entry_ref_id"),
    ]
    keys = set()
    with conn.cursor() as cur:
        for name, sql in queries:
            cur.execute("SAVEPOINT protected_keys")
            try:
                cur.execute(sql)
                found = {(f, i) for f, i in cur.fetchall()}
                cur.execute("RELEASE SAVEPOINT protected_keys")
                print(f"  {len(found)} filas con registros en {name} (se protegen)")
                keys |= found
            except psycopg2.Error as e:
                cur.execute("ROLLBACK TO SAVEPOINT protected_keys")
                print(f"  [!] no pude leer {name} ({e.__class__.__name__}); "
                      f"se asume sin registros", file=sys.stderr)
    return keys


# ---------------------------------------------------------------------------
# Re-keying: detect rows whose text moved to another entry_id in the same file
# ---------------------------------------------------------------------------

def _id_sort_key(entry_id):
    """Numeric ids sort numerically ('99' < '100'); anything else after them."""
    return (0, int(entry_id), "") if entry_id.isdigit() else (1, 0, entry_id)


def similarity(a, b):
    """0..1 text similarity, used to tell a small edit of the same line
    (typo, a number, spacing) from a slot that now holds different content."""
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _has_es(row):
    """row = (ja, en, es)"""
    return bool(row[2] and row[2].strip())


def _has_work(file_key, entry_id, row, protected_keys):
    """A row is worth protecting if it has a translation, history or proposals."""
    return _has_es(row) or (file_key, entry_id) in protected_keys


def plan_rekeys(local_rows, supabase_state, strict=False, protected_keys=frozenset(),
                min_similarity=0.5):
    """Pure function (no DB access). Decides what to do with rows whose
    entry_id changed or whose slot now holds different text. Returns a dict:

      moves    : [(file, old_id, new_id, kind)]   re-key a row that has work
      parked   : [(file, old_id, park_id, why)]   displaced row WITH work, renamed away
      deleted  : [(file, entry_id)]               displaced row WITHOUT work, removed
      replaced : [(file, entry_id, ja, en)]       empty row whose slot has new text:
                                                  ja/en overwritten in place

    "work" = a translation (es not empty), any entry_history row or any
    pending/past proposal (protected_keys). A row with work is never deleted or overwritten.

    Stage 1 (re-key): per file,
      placed     = Supabase rows whose (file, entry_id) exists locally with the
                   same ja -> already where they belong.
      candidates = every other row WITH work (old id, ja changed, parked...).
      slots      = local entries that need a row: id missing in Supabase, id
                   taken by a row with a different ja, or id holding an EMPTY
                   row (e.g. inserted by a previous sync).
      Candidates are matched to slots by (ja, en), then by ja alone.
    Stage 2 (what's left): any local id whose Supabase row still has a
      different ja can't keep it -- the exported JSON would write the old
      text into a slot the game now uses for something else:
        - row without work        -> ja/en overwritten in place ("replaced")
        - row with work, text very different (similarity < min_similarity)
                                  -> parked, a fresh row is inserted
        - row with work, text similar (typo / number edit)
                                  -> left alone, goes to the review CSV
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

        def work(i):
            return _has_work(file_key, i, supa[i], protected_keys)

        placed = {i for i, row in supa.items() if i in local and local[i][0] == row[0]}
        candidates = {i: row for i, row in supa.items() if i not in placed and work(i)}
        if not candidates:
            continue

        slots = {}
        for i, (ja, en) in local.items():
            if i not in supa or i not in placed or not work(i):
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
    # itself) has to be displaced: deleted if it has no work, parked if it has.
    moving_from = {(f, old) for f, old, _new, _k in moves}
    taken = set(supabase_state.keys())
    parked, deleted = [], []

    def park_id_for(file_key, entry_id):
        n = 0
        park_id = f"{PARKED_PREFIX}{entry_id}"
        while (file_key, park_id) in taken:
            n += 1
            park_id = f"{PARKED_PREFIX}{entry_id}_{n}"
        taken.add((file_key, park_id))
        return park_id

    for file_key, _old, new, _kind in moves:
        occupant_key = (file_key, new)
        if occupant_key not in supabase_state or occupant_key in moving_from:
            continue
        moving_from.add(occupant_key)
        if _has_work(file_key, new, supabase_state[occupant_key], protected_keys):
            parked.append((file_key, new, park_id_for(file_key, new), "ocupado"))
        else:
            deleted.append((file_key, new))

    # ---- Stage 2: slots that still hold a different ja after the moves ----
    post = dict(supabase_state)
    _apply_structural(post, moves, parked, deleted)

    replaced = []
    for file_key, entry_id, ja, en in local_rows:
        key = (file_key, entry_id)
        row = post.get(key)
        if row is None or row[0] == ja:
            continue
        if not _has_work(file_key, entry_id, row, protected_keys):
            replaced.append((file_key, entry_id, ja, en))
        elif similarity(row[0], ja) < min_similarity:
            parked.append((file_key, entry_id, park_id_for(file_key, entry_id), "contenido distinto"))
        # else: similar text + work -> stays, reported in the review CSV

    return {"moves": moves, "parked": parked, "deleted": deleted, "replaced": replaced}


def _apply_structural(state, moves, parked, deleted):
    """Deletes, re-keys and parks on a {(file, entry_id): row} dict."""
    for key in deleted:
        state.pop(key)

    relocations = []
    for file_key, old, new, _kind in moves:
        relocations.append(((file_key, new), state.pop((file_key, old))))
    for file_key, old, park_id, _why in parked:
        if (file_key, old) in state:      # stage-2 parks may target a row already handled
            relocations.append(((file_key, park_id), state.pop((file_key, old))))

    for key, row in relocations:
        if key in state:
            raise RuntimeError(f"Colision inesperada al reubicar filas: {key}")
        state[key] = row


def apply_plan_to_state(state, plan):
    """Mirror the plan on the in-memory Supabase snapshot, so the normal
    new/EN-update/JA-changed comparison that follows sees the post-plan world."""
    _apply_structural(state, plan["moves"], plan["parked"], plan["deleted"])
    for file_key, entry_id, ja, en in plan["replaced"]:
        _old_ja, _old_en, es = state[(file_key, entry_id)]
        state[(file_key, entry_id)] = (ja, en, es)


def _flat(text, limit=70):
    return " ".join(str(text).split())[:limit]


def plan_to_report_rows(plan, supabase_state_before):
    """Flat rows for the audit CSV. Must be called BEFORE apply_plan_to_state."""
    rows = []
    for file_key, old, new, kind in plan["moves"]:
        ja, en, es = supabase_state_before[(file_key, old)]
        rows.append({"accion": "mover", "file": file_key, "entry_id_viejo": old,
                     "entry_id_nuevo": new, "ja": ja, "en": en, "es": es, "detalle": kind})
    for file_key, old, park_id, why in plan["parked"]:
        ja, en, es = supabase_state_before[(file_key, old)]
        detail = ("ocupaba un id que necesita otra fila; tiene ES o historial, no se borra"
                  if why == "ocupado" else
                  "el id ahora tiene un texto muy distinto; tiene ES o historial, no se borra")
        rows.append({"accion": "aparcar", "file": file_key, "entry_id_viejo": old,
                     "entry_id_nuevo": park_id, "ja": ja, "en": en, "es": es, "detalle": detail})
    for file_key, entry_id in plan["deleted"]:
        ja, en, es = supabase_state_before[(file_key, entry_id)]
        rows.append({"accion": "borrar", "file": file_key, "entry_id_viejo": entry_id,
                     "entry_id_nuevo": "", "ja": ja, "en": en, "es": es,
                     "detalle": "fila sin ES ni historial que ocupaba un id necesario"})
    for file_key, entry_id, new_ja, _new_en in plan["replaced"]:
        ja, en, es = supabase_state_before[(file_key, entry_id)]
        rows.append({"accion": "reemplazar", "file": file_key, "entry_id_viejo": entry_id,
                     "entry_id_nuevo": entry_id, "ja": ja, "en": en, "es": es,
                     "detalle": f"fila sin ES ni historial; JA/EN sobrescritos en sitio con: {_flat(new_ja)}"})
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
      0. delete displaced rows without work (frees their ids)
      A. every moving/parked row goes to a unique temporary or park id
         (this vacates all old ids, so cycles and swaps can't collide
         with the UNIQUE(file, entry_id) constraint)
      B. moving rows go from their temporary id to their final id
      C. rows without work get their ja/en overwritten in place
    Renames rely on entry_history's FK being ON UPDATE CASCADE.
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
    phase_a += [(f, old, park_id) for f, old, park_id, _why in plan["parked"]]
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

    if plan["replaced"]:
        _run_batches(
            cur,
            "UPDATE entries AS e SET ja = v.ja, en = v.en "
            "FROM (VALUES %s) AS v(file, entry_id, ja, en) "
            "WHERE e.file = v.file AND e.entry_id = v.entry_id",
            plan["replaced"],
        )


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
    parser.add_argument("--similarity", type=float, default=0.5,
                         help="0-1. When a slot now holds different JA text and the old row has a translation: "
                              "below this similarity the old row is parked and a fresh row is inserted; at or "
                              "above it (typo / number edit) the row is left for manual review (default: 0.5)")
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
    history_keys = fetch_history_keys(conn)
    plan = plan_rekeys(local_rows, supabase_state, strict=args.strict,
                       protected_keys=history_keys, min_similarity=args.similarity)
    rekey_report = plan_to_report_rows(plan, supabase_state)
    n_moves, n_parked, n_deleted = len(plan["moves"]), len(plan["parked"]), len(plan["deleted"])
    n_replaced = len(plan["replaced"])
    n_ambiguous = sum(1 for m in plan["moves"] if "ambiguo" in m[3])
    print(f"  {n_moves} filas a reubicar ({n_ambiguous} ambiguas), "
          f"{n_parked} con ES/historial aparcadas, {n_deleted} vacias borradas, "
          f"{n_replaced} vacias con JA/EN reemplazados en sitio")
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
                "es": _old_es, "similitud": f"{similarity(old_ja, ja):.2f}",
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
                    print(f"Reubicando filas ({n_moves} movidas, {n_parked} aparcadas, "
                          f"{n_deleted} borradas, {n_replaced} reemplazadas)...")
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
            writer = csv.DictWriter(f, fieldnames=["file", "entry_id", "ja_old", "ja_new", "en_old", "en_new",
                                                      "es", "similitud"])
            writer.writeheader()
            writer.writerows(ja_changed_rows)

    print(f"\nFilas reubicadas a su nuevo entry_id (con su ES): {n_moves}")
    if n_parked or n_deleted:
        print(f"  Desplazadas: {n_parked} con ES/historial aparcadas como '{PARKED_PREFIX}*', {n_deleted} vacias borradas")
    if n_replaced:
        print(f"Filas sin traduccion cuyo id ahora tiene otro texto (JA/EN sobrescritos en sitio): {n_replaced}")
    print(f"Nuevas entradas insertadas: {len(new_rows)}")
    print(f"Referencias EN actualizadas automaticamente (JA sin cambios): {len(en_updates)}")
    print(f"Entradas sin cambios: {unchanged_count}")
    print(f"Entradas con JA modificado y traduccion/historial, texto parecido (revisar a mano): {len(ja_changed_rows)}"
          + (f" -- detalle en {args.review_output}" if ja_changed_rows else ""))
    if ja_changed_rows:
        print("Estas NO se tocaron en Supabase -- su ES y estado actual siguen intactos. "
              "Suelen ser correcciones menores (typo, un numero): revisa el CSV (columna 'es') "
              "y decide si la traduccion sigue valiendo.")


if __name__ == "__main__":
    main()
