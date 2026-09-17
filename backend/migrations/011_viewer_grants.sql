-- ============================================================
-- 011_viewer_grants.sql
-- Dreamboat Slabs — read-only dashboard access for a second person.
--
-- WHY THIS EXISTS (2026-09-17)
--   Brady wants someone else to be able to VIEW his cards. Adding that email
--   to ALLOWED_EMAILS is NOT enough and is the trap worth naming: it gets them
--   past the API's 403, and then they see an EMPTY dashboard.
--
--   Every row is stamped with Brady's user_id, and `cards_owner` (migration
--   002) reads:
--       USING (user_id = auth.uid())
--   A second Google account has a different auth.uid(), so Postgres returns
--   zero rows. The request succeeds, the page renders, nothing is there. That
--   looks like a bug, not a permission problem — which is exactly the kind of
--   failure that burns an afternoon.
--
-- WHAT THIS DOES
--   Adds a `viewer_grants` table and a SELECT-only policy per user-scoped
--   table. Read access becomes "I own the row OR someone granted me sight of
--   it." Write access is untouched.
--
-- 🔒 WHY THIS CANNOT ACCIDENTALLY GRANT WRITES
--   Postgres combines multiple PERMISSIVE policies with OR, but it does so
--   PER COMMAND. The existing `cards_owner` policy is FOR ALL; the policies
--   below are FOR SELECT. So:
--       SELECT -> owner OR viewer
--       INSERT / UPDATE / DELETE -> owner only, unchanged
--   The read-only guarantee is structural. A viewer cannot write even by
--   calling PostgREST directly with their own token, and no application code
--   is involved in enforcing it.
--
-- ⚠️ GRANTS ARE BY EMAIL, NOT user_id — ON PURPOSE
--   A person has no auth.uid() until they sign in for the first time, so
--   granting by user_id would require them to log in before they could be
--   given access, which is backwards. Email is matched from the JWT claim.
--   Emails are stored lowercased and compared lowercased (citext isn't enabled
--   on this project).
--
-- 🔴 THE SUBTLE TRAP THIS MIGRATION HANDLES
--   The policies below do `EXISTS (SELECT 1 FROM viewer_grants ...)`. That
--   subquery runs as the CALLING user, so it is itself subject to RLS on
--   viewer_grants. If the viewer cannot SELECT their own grant row, EXISTS
--   returns false and the whole feature silently does nothing — with no error
--   anywhere. Hence `viewer_grants_visible_to_viewer` below. Do not remove it.
--
-- IDEMPOTENT — safe to re-run.
-- ============================================================

-- ---------- the grant table ----------
create table if not exists viewer_grants (
  id            uuid primary key default gen_random_uuid(),
  owner_user_id uuid not null references auth.users(id) on delete cascade,
  viewer_email  text not null check (viewer_email = lower(btrim(viewer_email))
                                     and viewer_email <> ''),
  note          text,
  created_at    timestamptz not null default now(),
  unique (owner_user_id, viewer_email)
);

comment on table viewer_grants is
  'Read-only dashboard access. A row means viewer_email may SELECT owner_user_id''s rows. Never grants writes.';
comment on column viewer_grants.viewer_email is
  'Lowercased email, matched against the JWT email claim. Stored by email because a viewer has no user_id until first login.';

create index if not exists viewer_grants_viewer_idx on viewer_grants (viewer_email);
create index if not exists viewer_grants_owner_idx  on viewer_grants (owner_user_id);

-- ---------- RLS on the grant table itself ----------
alter table viewer_grants enable row level security;

-- The owner manages their own grants.
drop policy if exists "viewer_grants_owner" on viewer_grants;
create policy "viewer_grants_owner" on viewer_grants for all to authenticated
  using (owner_user_id = auth.uid())
  with check (owner_user_id = auth.uid());

-- 🔴 LOAD-BEARING. Without this the EXISTS subqueries below evaluate to false
-- for the very person they are meant to authorise, and the feature fails
-- silently. A viewer may read ONLY the rows that name them.
drop policy if exists "viewer_grants_visible_to_viewer" on viewer_grants;
create policy "viewer_grants_visible_to_viewer" on viewer_grants for select to authenticated
  using (viewer_email = lower(auth.jwt() ->> 'email'));

-- ---------- helper ----------
-- Centralises the grant check so the per-table policies stay readable and a
-- future change happens in one place rather than five.
create or replace function has_viewer_grant(p_owner uuid)
returns boolean
language sql
stable
as $$
  select exists (
    select 1 from viewer_grants g
    where g.owner_user_id = p_owner
      and g.viewer_email = lower(auth.jwt() ->> 'email')
  );
$$;

comment on function has_viewer_grant(uuid) is
  'True when the calling user has been granted read access to p_owner''s rows. Deliberately NOT security definer — it relies on viewer_grants_visible_to_viewer.';

-- ---------- SELECT-only viewer policies ----------
drop policy if exists "cards_viewer_read" on cards;
create policy "cards_viewer_read" on cards for select to authenticated
  using (has_viewer_grant(user_id));

drop policy if exists "grading_viewer_read" on grading_submissions;
create policy "grading_viewer_read" on grading_submissions for select to authenticated
  using (has_viewer_grant(user_id));

drop policy if exists "incoming_viewer_read" on incoming_shipments;
create policy "incoming_viewer_read" on incoming_shipments for select to authenticated
  using (has_viewer_grant(user_id));

drop policy if exists "trades_viewer_read" on trades;
create policy "trades_viewer_read" on trades for select to authenticated
  using (has_viewer_grant(user_id));

-- trade_items has no user_id; ownership comes from the parent trade.
drop policy if exists "trade_items_viewer_read" on trade_items;
create policy "trade_items_viewer_read" on trade_items for select to authenticated
  using (exists (select 1 from trades t
                 where t.id = trade_items.trade_id
                   and has_viewer_grant(t.user_id)));

-- ============================================================
-- GRANT ACCESS TO SOMEONE
--   Run as the OWNER (signed in), or from the SQL editor with the owner's id:
--
--     insert into viewer_grants (owner_user_id, viewer_email, note)
--     values ('1abe03a8-ae6a-47c7-8b2f-0f3d719f6596',
--             'person@example.com',
--             'read-only dashboard access')
--     on conflict (owner_user_id, viewer_email) do nothing;
--
-- REVOKE
--     delete from viewer_grants
--     where owner_user_id = '1abe03a8-ae6a-47c7-8b2f-0f3d719f6596'
--       and viewer_email = 'person@example.com';
--
-- ⚠️ STILL REQUIRED OUTSIDE THIS MIGRATION — the grant alone is not enough:
--   1. Railway  -> ALLOWED_EMAILS must include the viewer, or the API 403s
--                  before RLS is ever consulted.
--   2. Netlify  -> VITE_ALLOWED_EMAIL must include the viewer (it already
--                  splits on commas), or the frontend signs them straight out.
--   3. Backend  -> READONLY_EMAILS should list the viewer so the API refuses
--                  writes outright. RLS already stops them writing to BRADY's
--                  rows, but without this a viewer could still create rows of
--                  THEIR OWN, which is harmless but confusing.
--
-- VERIFY (as the viewer, after signing in once):
--     select count(*) from cards;              -- should be Brady's count
--     insert into cards (player, category) values ('x','other');  -- must FAIL
-- ============================================================
