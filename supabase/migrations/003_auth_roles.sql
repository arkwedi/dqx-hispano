-- 003_auth_roles.sql
-- Run in the Supabase SQL Editor, after 001_schema.sql and 002_rls_anon_access.sql.
--
-- Adds a role hierarchy on top of Supabase Auth:
--   pending    -- just signed up, no write access at all (blocks bots/randoms
--                 by default -- an admin/editor must promote them)
--   suggester  -- can browse and "suggest" an ES translation, but it does NOT
--                 write to entries directly -- it creates a row in
--                 translation_proposals, awaiting review
--   editor     -- can edit entries directly (like before), and can
--                 approve/reject suggester proposals
--   admin      -- everything editor can do, plus can promote/demote roles
--                 (pending -> suggester -> editor)
--
-- Anonymous browsing (SELECT on entries) is left exactly as-is from
-- 002_rls_anon_access.sql -- this migration only adds WRITE-side rules.

-- ─────────────────────────────────────────────────────────────────────────
-- profiles: one row per Supabase Auth user
-- ─────────────────────────────────────────────────────────────────────────

create table if not exists profiles (
    id           uuid primary key references auth.users(id) on delete cascade,
    display_name text,
    role         text not null default 'pending'
                 check (role in ('pending', 'suggester', 'editor', 'admin')),
    created_at   timestamptz not null default now(),
    approved_by  uuid references auth.users(id),
    approved_at  timestamptz
);

-- Auto-create a 'pending' profile row whenever someone signs up via
-- Supabase Auth. This is what makes the "pending by default" behavior work
-- without any client-side code having to remember to do it.
create or replace function public.handle_new_user()
returns trigger as $$
begin
    insert into public.profiles (id, display_name)
    values (new.id, new.raw_user_meta_data->>'display_name');
    return new;
end;
$$ language plpgsql security definer set search_path = public;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- Security-definer helper: lets RLS policies check "what's MY role" without
-- querying profiles directly from within a profiles policy (that causes
-- infinite recursion in Postgres RLS -- this is the standard way around it).
create or replace function public.my_role()
returns text as $$
    select role from public.profiles where id = auth.uid();
$$ language sql stable security definer set search_path = public;

alter table profiles enable row level security;

-- Anyone signed in can see their own profile (to know their own role/status).
create policy "users can read their own profile"
    on profiles for select
    to authenticated
    using (id = auth.uid());

-- editor/admin can see everyone's profile (to review pending signups and
-- know who's who).
create policy "editor/admin can read all profiles"
    on profiles for select
    to authenticated
    using (public.my_role() in ('editor', 'admin'));

-- Only admin can change roles (promote pending->suggester->editor, etc).
-- Deliberately does NOT let editors change roles, per the hierarchy you
-- described (editors can see who's asking, only admin approves).
create policy "admin can update roles"
    on profiles for update
    to authenticated
    using (public.my_role() = 'admin')
    with check (public.my_role() = 'admin');

-- Users can update their own display_name, but NOT their own role (the
-- WITH CHECK below blocks any row where the role differs from what a
-- fresh lookup says it currently is, so self-promotion via a crafted
-- update is not possible even though the row is "yours").
create policy "users can update their own display_name"
    on profiles for update
    to authenticated
    using (id = auth.uid())
    with check (id = auth.uid() and role = (select role from profiles where id = auth.uid()));

-- ─────────────────────────────────────────────────────────────────────────
-- translation_proposals: what a 'suggester' creates instead of writing
-- directly to entries
-- ─────────────────────────────────────────────────────────────────────────

create table if not exists translation_proposals (
    id           bigint generated always as identity primary key,
    entry_ref_id bigint not null references entries(id) on delete cascade,
    proposed_es  text not null,
    proposed_by  uuid not null references auth.users(id),
    status       text not null default 'pending'
                 check (status in ('pending', 'approved', 'rejected')),
    review_note  text,
    reviewed_by  uuid references auth.users(id),
    reviewed_at  timestamptz,
    created_at   timestamptz not null default now()
);

create index if not exists idx_proposals_status on translation_proposals (status);
create index if not exists idx_proposals_entry on translation_proposals (entry_ref_id);

alter table translation_proposals enable row level security;

-- suggester/editor/admin can submit a proposal (must be submitting as
-- themselves, can't forge proposed_by as someone else).
create policy "suggester+ can create proposals"
    on translation_proposals for insert
    to authenticated
    with check (
        public.my_role() in ('suggester', 'editor', 'admin')
        and proposed_by = auth.uid()
    );

-- suggesters can see their OWN proposals only (to check if it was approved
-- or rejected). editor/admin see everything, to review the queue.
create policy "suggester can read own proposals"
    on translation_proposals for select
    to authenticated
    using (
        proposed_by = auth.uid()
        or public.my_role() in ('editor', 'admin')
    );

-- Only editor/admin can update a proposal (approve/reject) -- see the
-- approve_proposal() function below, which is the intended way to do this
-- (keeps the entries.es update and the proposal status update atomic).
create policy "editor/admin can update proposals"
    on translation_proposals for update
    to authenticated
    using (public.my_role() in ('editor', 'admin'))
    with check (public.my_role() in ('editor', 'admin'));

-- ─────────────────────────────────────────────────────────────────────────
-- entries: tighten write access to editor/admin only. Suggesters lose
-- direct write access entirely -- they must go through proposals.
-- Anonymous/pending READ access is untouched (still governed by
-- 002_rls_anon_access.sql).
-- ─────────────────────────────────────────────────────────────────────────

drop policy if exists "anyone with the link can update entries" on entries;

create policy "editor/admin can update entries directly"
    on entries for update
    to authenticated
    using (public.my_role() in ('editor', 'admin'))
    with check (public.my_role() in ('editor', 'admin'));

-- ─────────────────────────────────────────────────────────────────────────
-- approve_proposal(): the one correct way to resolve a proposal. Runs as
-- a single atomic operation so a proposal can never end up "approved" in
-- the proposals table while entries.es was never actually updated (or
-- vice versa).
-- ─────────────────────────────────────────────────────────────────────────

create or replace function public.approve_proposal(
    p_proposal_id bigint,
    p_approve boolean,
    p_review_note text default null
)
returns void as $$
declare
    v_entry_id bigint;
    v_proposed_es text;
begin
    if public.my_role() not in ('editor', 'admin') then
        raise exception 'Solo editor/admin pueden resolver propuestas';
    end if;

    select entry_ref_id, proposed_es into v_entry_id, v_proposed_es
    from translation_proposals
    where id = p_proposal_id and status = 'pending';

    if v_entry_id is null then
        raise exception 'Propuesta % no existe o ya fue resuelta', p_proposal_id;
    end if;

    update translation_proposals
    set status = case when p_approve then 'approved' else 'rejected' end,
        review_note = p_review_note,
        reviewed_by = auth.uid(),
        reviewed_at = now()
    where id = p_proposal_id;

    if p_approve then
        update entries
        set es = v_proposed_es,
            status = 'traducido',
            updated_by = (select display_name from profiles where id = auth.uid()),
            updated_at = now()
        where id = v_entry_id;
    end if;
end;
$$ language plpgsql security definer set search_path = public;
