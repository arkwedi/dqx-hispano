-- Run this after supabase_schema.sql.
-- Since collaborators will use a shared link with no login, RLS needs to
-- allow the "anon" role (the public API key) to read and update entries,
-- not just "authenticated" users.
--
-- IMPORTANT TRADE-OFF: anyone with the link (and therefore the anon key
-- embedded in the frontend) can edit any row. That's the deal you're
-- explicitly choosing for now to keep onboarding collaborators frictionless.
-- Inserts/deletes stay blocked for anon, same reasoning as before.

drop policy if exists "authenticated can read entries" on entries;
drop policy if exists "authenticated can update entries" on entries;

create policy "anyone with the link can read entries"
    on entries for select
    to anon, authenticated
    using (true);

create policy "anyone with the link can update entries"
    on entries for update
    to anon, authenticated
    using (true)
    with check (true);

-- Lightweight view for the file picker dropdown in the frontend, so it
-- doesn't have to scan/deduplicate 269k rows client-side just to list the
-- ~1,071 distinct file names.
create or replace view file_list as
    select file, count(*) as entry_count
    from entries
    group by file
    order by file;

grant select on file_list to anon, authenticated;
