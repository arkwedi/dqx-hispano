#!/usr/bin/env python3
"""
bulk_mt_translate.py

Fills empty ES translations in Supabase with a DeepL machine-translation
DRAFT -- never marks them as human-reviewed. Rows get status='borrador_mt'
(not 'traducido'), so the frontend and everyone on the team can tell "this
is a DeepL first pass, please clean it up" apart from a truly blank line.

This replaces dqx_translate_es.py's file-based input/output with Supabase
directly: it reads rows where es='' AND status='pendiente', and writes the
draft straight back to the es column. Since state lives in Supabase, this
is naturally resumable -- re-running only picks up rows that are still
'pendiente' (nothing already drafted or human-translated gets touched or
re-sent to the API).

SCOPED BY DEFAULT: DeepL's Free API has a hard monthly character quota
(500,000 chars). Translating every file blind risks burning the whole
quota on one unexpectedly huge file. So:

    --file NAME       (recommended starting point) translate ONE source
                       file only, e.g. eventTextCsA11Client
    --files A B C      translate a specific handful of files
    --all              translate everything pending -- requires --force,
                       since this is the "I understand the quota risk" flag

Before calling DeepL at all, this always estimates the total characters
the selected scope would use and compares it against your remaining DeepL
quota. If the estimate exceeds what's left, it aborts (use --force to
proceed anyway). Use --dry-run to see the estimate without spending
anything or touching Supabase.

Requires: pip install deepl psycopg2-binary

Usage:
    export DATABASE_URL="postgresql://...supabase connection string..."
    export DEEPL_API_KEY="..."

    # Recommended first run: one file, see the estimate, confirm it looks right
    python bulk_mt_translate.py --file eventTextCsA11Client --dry-run

    # Actually translate that one file
    python bulk_mt_translate.py --file eventTextCsA11Client

    # A specific handful
    python bulk_mt_translate.py --files eventTextCsA11Client eventTextCsA12Client

    # Everything pending (deliberate, requires --force)
    python bulk_mt_translate.py --all --force

    # With Clarity's glossary applied for proper nouns (JA source only)
    python bulk_mt_translate.py --file eventTextCsA11Client --source JA --glossary glossary.db
"""

import argparse
import os
import re
import sqlite3
import sys
import time

try:
    import deepl
except ImportError:
    sys.exit("deepl no instalado. Ejecuta:  pip install deepl")

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    sys.exit("psycopg2 no instalado. Ejecuta:  pip install psycopg2-binary")

# ─────────────────────────────────────────────────────────────────────────────
# Configuracion
# ─────────────────────────────────────────────────────────────────────────────

LINE_WIDTH = 46      # same as Clarity (wrap_width=46 in dialogue.py)
BATCH_SIZE = 40       # strings per DeepL call (Free tier max: 50)
API_DELAY = 0.35      # seconds between calls
MAX_CHAR = 4000       # truncate absurdly long strings before sending
DEEPL_GLOSSARY_NAME = "DQX_Glossary"

# ─────────────────────────────────────────────────────────────────────────────
# Custom instructions (from dqxclarity's own deepl.py, adapted per language)
# ─────────────────────────────────────────────────────────────────────────────

INSTRUCTIONS_ES = [
    "You are an expert translator and cultural localization specialist with deep "
    "knowledge of video game localization. Translate the provided Dragon Quest X text "
    "into Spanish. Preserve the original tone, humor, personality, and emotional "
    "nuances of the dialogue, considering the unique style and atmosphere of Dragon Quest X.",
    "Adapt idioms, cultural references, and wordplay to resonate naturally with native "
    "Spanish speakers while maintaining the fantasy RPG context. Avoid overuse of "
    "profanity. Don't use the same word over and over.",
    "Maintain consistency in character voices, terminology, and naming conventions "
    "specific to Dragon Quest X throughout the translation.",
    "Avoid literal translations that may lose the original intent or impact, especially "
    "for game-specific terms or lore elements. Output only standard Latin characters — "
    "no accented vowels (use a e i o u without accents), no n-tilde (use n instead), "
    "in case you find a word that uses 'ñ', try to replace it with a synonym that does not use 'ñ',"
    "you could also paraphrase the entire sentence to make it work with that restriction,"
    "no inverted punctuation (no exclamation or question marks at the start of sentences).",
    "Ensure the translation flows naturally and reads as if it were originally written "
    "in Spanish, while staying true to the game's narrative style.",
    "Consider the context and subtext of the dialogue, including any references to the "
    "game's lore, world, or ongoing storylines.",
    "If a word has been translated in a specific way, maintain that translation "
    "consistently. Respect established localization choices for Dragon Quest X.",
    "Pay attention to formal/informal speech patterns and adjust for Spanish, "
    "considering the speaker's role and status within the game world.",
    "Keep translations concise — approximately 46 characters per line maximum.",
    "Preserve game-specific jargon, spell names, and technical terms. Do NOT translate "
    "proper nouns like character names, place names, or skill names.",
]


def get_instructions(target_lang: str) -> list:
    return INSTRUCTIONS_ES  # this script is ES-focused; extend here if you add more languages


# ─────────────────────────────────────────────────────────────────────────────
# Placeholder system — identical to Clarity's translate.py __swap_placeholder_tags()
# ─────────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_MAP = [
    ("<pc_hiryu>", "<&13_aaaaaaa>"),
    ("<cs_pchero_hiryu>", "<&13_aaaaaab>"),
    ("<cs_pchero_race>", "<&8_aaa>"),
    ("<cs_pchero>", "<&13_aaaaaac>"),
    ("<kyodai_rel1>", "<&7_aa>"),
    ("<kyodai_rel2>", "<&7_ab>"),
    ("<kyodai_rel3>", "<&7_ac>"),
    ("<pc_hometown>", "<&8_aab>"),
    ("<pc_race>", "<&8_aac>"),
    ("<%sM_real_race>", "<&8_aad>"),
    ("<pc_rel1>", "<&7_ad>"),
    ("<pc_rel2>", "<&7_ae>"),
    ("<pc_rel3>", "<&7_af>"),
    ("<kyodai>", "<&13_aaaaaad>"),
    ("<pc>", "<&13_aaaaaae>"),
    ("<client_pcname>", "<&13_aaaaaaf>"),
    ("<heart>", "<&2a>"),
    ("<diamond>", "<&2b>"),
    ("<spade>", "<&2c>"),
    ("<clover>", "<&2d>"),
    ("<r_triangle>", "<&2e>"),
    ("<l_triangle>", "<&2f>"),
    ("<half_star>", "<&2g>"),
    ("<null_star>", "<&2h>"),
    ("<npc>", "<&13_aaaaaag>"),
    ("<pc_syokugyo>", "<&13_aaaaaah>"),
    ("<pc_original>", "<&13_aaaaaai>"),
    ("<log_pc>", "<&13_aaaaaaj>"),
    ("<%sM_NAME>", "<&13_aaaaaak>"),
    ("<%sM_BEFORE_NAME>", "<&13_aaaaaal>"),
    ("<%sM_OWNER_OTHER>", "<&13_aaaaaam>"),
    ("<%sM_OWNER>", "<&13_aaaaaan>"),
    ("<%sM_SAMA>", "<&6_a>"),
]


def swap_placeholders(text: str) -> str:
    for original, token in _PLACEHOLDER_MAP:
        text = text.replace(original, token)
    return text


def restore_placeholders(text: str) -> str:
    for original, token in reversed(_PLACEHOLDER_MAP):
        text = text.replace(token, original)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Word-wrap (same logic as Clarity's __wrap_text, width=46)
# ─────────────────────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")


def _visible_len(s: str) -> int:
    return len(_TAG_RE.sub("", s))


def wrap_text(text: str, width: int = LINE_WIDTH) -> str:
    result = []
    for paragraph in text.split("\n"):
        if _visible_len(paragraph) <= width:
            result.append(paragraph)
            continue
        tokens = re.findall(r"<[^>]+>|\S+", paragraph)
        current = ""
        cur_vis = 0
        for token in tokens:
            tv = _visible_len(token)
            sv = 1 if current else 0
            if cur_vis + sv + tv <= width:
                current += (" " if current else "") + token
                cur_vis += sv + tv
            else:
                if current:
                    result.append(current)
                current = token
                cur_vis = tv
        if current:
            result.append(current)
    return "\n".join(result)


# ─────────────────────────────────────────────────────────────────────────────
# Spanish postprocessing (no tildes, no n-tilde — smart synonyms)
# ─────────────────────────────────────────────────────────────────────────────

_TILDE_MAP = str.maketrans("áéíóúÁÉÍÓÚüÜ", "aeiouAEIOUuU")

_N_TILDE_FIXES = [
    (re.compile(r"\bniñas\b", re.I), "chicas"),
    (re.compile(r"\bniños\b", re.I), "chicos"),
    (re.compile(r"\bniña\b", re.I), "chica"),
    (re.compile(r"\bniño\b", re.I), "chico"),
    (re.compile(r"\bextraños\b", re.I), "extranas"),
    (re.compile(r"\bextrañas\b", re.I), "extranas"),
    (re.compile(r"\bextraño\b", re.I), "extrano"),
    (re.compile(r"\bextraña\b", re.I), "extrana"),
    (re.compile(r"\bhace\s+(\w+)\s+años\b", re.I), r"hace \1 tiempo"),
    (re.compile(r"\b(\d+)\s+años\b", re.I), r"\1 primaveras"),
    (re.compile(r"\baños\b", re.I), "ciclos"),
    (re.compile(r"\baño\b", re.I), "ciclo"),
    (re.compile(r"\bsueños\b", re.I), "visiones"),
    (re.compile(r"\bsueño\b", re.I), "vision"),
    (re.compile(r"\bdaños\b", re.I), "perjuicios"),
    (re.compile(r"\bdaño\b", re.I), "perjuicio"),
    (re.compile(r"\bseñorita\b", re.I), "jovencita"),
    (re.compile(r"\bseñoras\b", re.I), "damas"),
    (re.compile(r"\bseñora\b", re.I), "dama"),
    (re.compile(r"\bseñores\b", re.I), "lords"),
    (re.compile(r"\bseñor\b", re.I), "lord"),
    (re.compile(r"\besta mañana\b", re.I), "esta manana"),
    (re.compile(r"\bla mañana\b", re.I), "la manana"),
    (re.compile(r"\bmañana\b", re.I), "al dia siguiente"),
    (re.compile(r"\bpequeñas\b", re.I), "pequenas"),
    (re.compile(r"\bpequeños\b", re.I), "pequenos"),
    (re.compile(r"\bpequeña\b", re.I), "pequena"),
    (re.compile(r"\bpequeño\b", re.I), "pequeno"),
    (re.compile(r"\bdueños\b", re.I), "amos"),
    (re.compile(r"\bdueño\b", re.I), "amo"),
    (re.compile(r"\bmontañas\b", re.I), "montanas"),
    (re.compile(r"\bmontaña\b", re.I), "montana"),
    (re.compile(r"\bcañon\b", re.I), "canon"),
    (re.compile(r"ñ", re.I), "n"),  # fallback
]


def postprocess_es(text: str) -> str:
    for pattern, repl in _N_TILDE_FIXES:
        text = pattern.sub(repl, text)
    text = text.translate(_TILDE_MAP)
    text = re.sub(r"[¿¡]", "", text)
    return re.sub(r"  +", " ", text).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Glossary (local Clarity-format glossary.db, e.g. the one export_translations.py
# --build-clarity-dbs produces)
# ─────────────────────────────────────────────────────────────────────────────

def load_local_glossary(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT ja, en FROM glossary WHERE ja != '' AND en != ''")
    result = dict(cur.fetchall())
    conn.close()
    print(f"Glosario local cargado: {len(result):,} terminos")
    return result


def apply_glossary(text: str, glossary: dict) -> str:
    """Same logic as Clarity's __glossify() -- replace JA terms with their
    mapped value before sending to DeepL."""
    for ja, mapped in glossary.items():
        text = text.replace(ja, f" {mapped} ")
    return re.sub(r"  +", " ", text).strip()


def get_deepl_glossary_id(translator, source_lang: str, target_lang: str, allow_create: bool, glossary_dict: dict):
    """By default (allow_create=False) only reuses an existing DeepL-side
    glossary if one is already set up -- doesn't auto-create one on every
    run. Pass --create-glossary-if-missing to opt into auto-creation."""
    name = f"{DEEPL_GLOSSARY_NAME}_{source_lang}_{target_lang}"
    for g in translator.list_glossaries():
        if (g.name == name
                and g.source_lang.upper() == source_lang.upper()
                and g.target_lang.upper().startswith(target_lang.upper().split("-")[0])):
            print(f"Reusing DeepL glossary: {g.glossary_id} ({g.entry_count} entries)")
            return g.glossary_id

    if not allow_create:
        print(f"No se encontro un glosario de DeepL llamado '{name}'. "
              f"Usa --create-glossary-if-missing si quieres que se cree automaticamente.")
        return None

    entries = {v: v for v in glossary_dict.values() if 2 <= len(v) <= 50 and re.search(r"[A-Za-z]", v)}
    if not entries:
        return None
    entries = dict(list(entries.items())[:4000])
    print(f"Creando glosario de DeepL '{name}' ({len(entries)} entradas)...")
    try:
        g = translator.create_glossary(name=name, source_lang=source_lang, target_lang=target_lang, entries=entries)
        print(f"Creado: {g.glossary_id}")
        return g.glossary_id
    except Exception as e:
        print(f"Aviso: no se pudo crear el glosario: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Translation
# ─────────────────────────────────────────────────────────────────────────────

def translate_batch(translator, texts, source_lang, target_lang, glossary_id, instructions):
    if not texts:
        return []

    protected = [swap_placeholders(t[:MAX_CHAR]) for t in texts]

    kwargs = dict(text=protected, source_lang=source_lang, target_lang=target_lang, preserve_formatting=True)
    if glossary_id:
        kwargs["glossary"] = glossary_id
    kwargs["context"] = " ".join(instructions[:3])

    try:
        results = translator.translate_text(**kwargs)
        translated = [r.text for r in results]
    except Exception as e:
        print(f"  DeepL ERROR: {e}")
        return list(texts)

    final = []
    for trans in translated:
        restored = restore_placeholders(trans)
        wrapped = wrap_text(restored)
        cleaned = postprocess_es(wrapped)
        final.append(cleaned)
    return final


# ─────────────────────────────────────────────────────────────────────────────
# Supabase
# ─────────────────────────────────────────────────────────────────────────────

def fetch_pending_rows(pg_conn, file_filter=None, files_filter=None):
    sql = "SELECT file, entry_id, ja, en FROM entries WHERE es = ''"
    params = []
    if file_filter:
        sql += " AND file = %s"
        params.append(file_filter)
    elif files_filter:
        sql += " AND file = ANY(%s)"
        params.append(files_filter)
    sql += " ORDER BY file, entry_id"

    rows = []
    with pg_conn.cursor() as cur:
        cur.execute(sql, params)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            rows.extend(batch)
    return rows


def estimate_chars(rows, source_lang, glossary_dict):
    total = 0
    for file_key, entry_id, ja, en in rows:
        source = en if (source_lang.startswith("EN") and en) else ja
        if glossary_dict and source_lang.startswith("JA"):
            source = apply_glossary(source, glossary_dict)
        total += len(source[:MAX_CHAR])
    return total


def write_drafts_batch(pg_conn, results):
    """results = list of (es_text, file, entry_id)"""
    if not results:
        return
    with pg_conn.cursor() as cur:
        execute_values(
            cur,
            "UPDATE entries AS e SET es = v.es, status = 'borrador_mt', "
            "updated_by = 'deepl_bulk_mt', updated_at = now() "
            "FROM (VALUES %s) AS v(es, file, entry_id) "
            "WHERE e.file = v.file AND e.entry_id = v.entry_id",
            results,
        )
    pg_conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--file", help="Translate only this source file (recommended starting point)")
    scope.add_argument("--files", nargs="+", metavar="FILE", help="Translate this specific set of files")
    scope.add_argument("--all", action="store_true", help="Translate every pending row -- requires --force")

    parser.add_argument("--force", action="store_true",
                         help="Required alongside --all. Also bypasses the quota estimate abort.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Show the character estimate and row count, call DeepL for nothing, write nothing")
    parser.add_argument("--api-key", default=None, help="DeepL API key (or set DEEPL_API_KEY env var)")
    parser.add_argument("--glossary", default=None, help="Path to a local Clarity-format glossary.db")
    parser.add_argument("--source", default="EN", help="Source language: EN (default) or JA")
    parser.add_argument("--lang", default="ES", help="Target language (default: ES)")
    parser.add_argument("--create-glossary-if-missing", action="store_true",
                         help="Auto-create the DeepL-side glossary if none exists yet (off by default)")
    args = parser.parse_args()

    if args.all and not args.force:
        sys.exit("[!] --all requires --force -- this is the 'I understand the quota risk' confirmation.")

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("[!] DATABASE_URL environment variable is not set.")

    api_key = args.api_key or os.environ.get("DEEPL_API_KEY")
    if not api_key:
        sys.exit("[!] No DeepL API key found. Pass --api-key or set DEEPL_API_KEY.")

    source_lang = args.source.upper()
    target_lang = args.lang.upper()

    glossary_dict = {}
    if args.glossary:
        if os.path.exists(args.glossary):
            glossary_dict = load_local_glossary(args.glossary)
        else:
            print(f"Aviso: no se encontro el glosario en {args.glossary}")

    print("Conectando a Supabase...")
    pg_conn = psycopg2.connect(db_url)
    pg_conn.autocommit = False

    rows = fetch_pending_rows(pg_conn, file_filter=args.file, files_filter=args.files)
    if not rows:
        print("No hay filas pendientes (es='') en el alcance elegido.")
        pg_conn.close()
        return

    est_chars = estimate_chars(rows, source_lang, glossary_dict)
    print(f"\nFilas pendientes en el alcance: {len(rows):,}")
    print(f"Caracteres estimados a enviar: {est_chars:,}")

    print("\nConectando a DeepL...")
    translator = deepl.Translator(api_key)
    try:
        usage = translator.get_usage()
        remaining = usage.character.limit - usage.character.count
        print(f"Cuota DeepL: {usage.character.count:,} / {usage.character.limit:,} usados, {remaining:,} restantes")
    except deepl.AuthorizationException:
        sys.exit("[!] DeepL API key invalida.")

    if est_chars > remaining and not args.force:
        sys.exit(
            f"\n[!] La estimacion ({est_chars:,} caracteres) supera tu cuota restante ({remaining:,}).\n"
            f"    Reduce el alcance (--file en vez de --files/--all) o usa --force si de verdad quieres continuar."
        )

    if args.dry_run:
        print("\n[dry-run] No se llamo a DeepL ni se escribio nada en Supabase.")
        pg_conn.close()
        return

    glossary_id = None
    if glossary_dict:
        glossary_id = get_deepl_glossary_id(translator, source_lang, target_lang, args.create_glossary_if_missing, glossary_dict)

    instructions = get_instructions(target_lang)

    print(f"\nTraduciendo {len(rows):,} filas en lotes de {BATCH_SIZE}...")
    translated_total = 0

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        sources = []
        for file_key, entry_id, ja, en in batch:
            source = en if (source_lang.startswith("EN") and en) else ja
            if glossary_dict and source_lang.startswith("JA"):
                source = apply_glossary(source, glossary_dict)
            sources.append(source)

        translated = translate_batch(translator, sources, source_lang, target_lang, glossary_id, instructions)

        results = [(es_text, file_key, entry_id) for (file_key, entry_id, ja, en), es_text in zip(batch, translated)]
        write_drafts_batch(pg_conn, results)
        translated_total += len(results)

        end = start + len(batch)
        pct = end / len(rows) * 100
        sample = translated[-1].replace("\n", " / ")[:55] if translated else ""
        print(f"  [{end:>5}/{len(rows)}] {pct:>5.1f}%  ->  {sample}")
        time.sleep(API_DELAY)

    pg_conn.close()

    print(f"\nListo: {translated_total} filas actualizadas con borrador de DeepL (status='borrador_mt').")
    try:
        usage = translator.get_usage()
        print(f"Uso final DeepL: {usage.character.count:,} / {usage.character.limit:,}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
