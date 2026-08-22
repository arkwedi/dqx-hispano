-- 004_block_pending_reads.sql
-- Run after 003_auth_roles.sql.
--
-- Tightens READ access too: previously (002_rls_anon_access.sql) anyone
-- with the link, logged in or not, could SELECT from entries and
-- file_list. This closes that -- only signed-in users with an approved
-- role (suggester/editor/admin) can read anything. 'pending' and
-- anonymous visitors get nothing until an admin promotes them.

drop policy if exists "anyone with the link can read entries" on entries;

create policy "approved users can read entries"
    on entries for select
    to authenticated
    using (public.my_role() in ('suggester', 'editor', 'admin'));

-- file_list (the view powering the file picker) was granted to anon +
-- authenticated in 002. By default, a Postgres view runs with the
-- permissions of whoever CREATED it, not the querying user -- so it does
-- NOT automatically inherit entries' RLS just because entries got
-- tightened. This must be set explicitly (Postgres 15+, which Supabase
-- runs) so the view re-checks RLS as the actual calling user:
alter view file_list set (security_invoker = true);

revoke select on file_list from anon;
grant select on file_list to authenticated;

-- With security_invoker on, file_list now correctly requires whatever
-- entries requires (approved role) for whoever queries it -- verify this
-- with a real signed-in-but-pending test user before trusting it.
