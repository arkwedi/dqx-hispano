-- DQX ES translation table -- run this in the Supabase SQL Editor.

-- Needed for fast ILIKE/partial-text search across ja/en/es (used by the
-- "search in JA+EN+ES at once" feature we sketched earlier).
create extension if not exists pg_trgm;

create table if not exists entries (
    id          bigint generated always as identity primary key,
    file        text not null,
    entry_id    text not null,
    ja          text not null,
    en          text not null default '',
    es          text not null default '',
    status      text not null default 'pendiente'
                check (status in ('pendiente', 'traducido', 'revisado')),
    updated_by  text,
    updated_at  timestamptz,
    unique (file, entry_id)
);

-- Speeds up "show me all rows for this file" in the spreadsheet view.
create index if not exists idx_entries_file on entries (file);

-- Speeds up the multi-language search (ILIKE '%term%') across all three columns.
create index if not exists idx_entries_ja_trgm on entries using gin (ja gin_trgm_ops);
create index if not exists idx_entries_en_trgm on entries using gin (en gin_trgm_ops);
create index if not exists idx_entries_es_trgm on entries using gin (es gin_trgm_ops);

-- Row Level Security: locked down by default, only signed-in collaborators
-- can read or write. Tighten/loosen these once you know how you'll invite
-- people (Supabase Auth email invites, magic links, etc).
alter table entries enable row level security;

create policy "authenticated can read entries"
    on entries for select
    to authenticated
    using (true);

create policy "authenticated can update entries"
    on entries for update
    to authenticated
    using (true)
    with check (true);

-- Nobody can insert/delete rows from the frontend for now -- rows only get
-- created by the migration script (using the service_role key, which
-- bypasses RLS). This avoids collaborators accidentally creating duplicate
-- or malformed entries.
