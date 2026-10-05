-- ============================================================
-- 011_viewer_grants.sql
-- Lets someone else view my dashboard without being able to change anything.
--
-- I want someone else to be able to see my cards. Just adding their email to
-- ALLOWED_EMAILS isn't enough. It gets them past the API, but then they see
-- an empty dashboard.
--
-- Every card has my user_id on it, and cards_owner (migration 002) is:
--       USING (user_id = auth.uid())
-- A different Google account has a different auth.uid(), so they get zero
-- rows. Nothing errors and the page loads, it's just empty. That looks like a
-- bug instead of a permissions thing.
--
-- What this does:
--   Adds a viewer_grants table and a read-only policy on each table. Now you
--   can read a row if you own it OR if I gave you access. Changing things is
--   still owner only.
--
-- Why this can't let them change anything:
--   Postgres combines policies with OR, but separately for each type of query.
--   cards_owner is FOR ALL and the new ones are FOR SELECT, so:
--       SELECT -> owner OR viewer
--       INSERT / UPDATE / DELETE -> owner only, same as before
--   This is all in the database, so a viewer can't change anything even if
--   they call Supabase directly with their own token.
--
-- Access goes by email, not user_id, on purpose:
--   Someone doesn't have a user_id until they sign in the first time, so they'd
--   have to log in before I could give them access. The email comes from their
--   login token. Emails are saved and compared in lowercase.
--
-- One thing to watch out for:
--   The policies below use EXISTS (SELECT 1 FROM viewer_grants ...). That runs
--   as the person logged in, so RLS applies to it too. If the viewer can't see
--   their own row in viewer_grants, EXISTS comes back false and nothing works,
--   with no error. That's why viewer_grants_visible_to_viewer is below. Don't
--   remove it.
--
-- Safe to run more than once.
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

-- I manage my own grants.
drop policy if exists "viewer_grants_owner" on viewer_grants;
create policy "viewer_grants_owner" on viewer_grants for all to authenticated
  using (owner_user_id = auth.uid())
  with check (owner_user_id = auth.uid());

-- Don't remove this. Without it, the EXISTS checks below come back false for
-- the exact person they're supposed to let in, and it fails with no error. A
-- viewer can only see the rows with their own email.
drop policy if exists "viewer_grants_visible_to_viewer" on viewer_grants;
create policy "viewer_grants_visible_to_viewer" on viewer_grants for select to authenticated
  using (viewer_email = lower(auth.jwt() ->> 'email'));

-- ---------- helper ----------
-- Puts the access check in one place so the policies below are easier to read
-- and I only have to change it once.
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

-- ---------- read-only viewer policies ----------
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

-- trade_items has no user_id, so it goes by who owns the trade.
drop policy if exists "trade_items_viewer_read" on trade_items;
create policy "trade_items_viewer_read" on trade_items for select to authenticated
  using (exists (select 1 from trades t
                 where t.id = trade_items.trade_id
                   and has_viewer_grant(t.user_id)));

-- ============================================================
-- To give someone access
--   Run while signed in as me, or from the SQL editor with my id:
--
--     insert into viewer_grants (owner_user_id, viewer_email, note)
--     values ('1abe03a8-ae6a-47c7-8b2f-0f3d719f6596',
--             'person@example.com',
--             'read-only dashboard access')
--     on conflict (owner_user_id, viewer_email) do nothing;
--
-- To take it away
--     delete from viewer_grants
--     where owner_user_id = '1abe03a8-ae6a-47c7-8b2f-0f3d719f6596'
--       and viewer_email = 'person@example.com';
--
-- The grant isn't enough by itself, these also have to be set:
--   1. Railway  -> ALLOWED_EMAILS needs the viewer, or the API blocks them
--                  before RLS even gets checked.
--   2. Netlify  -> VITE_ALLOWED_EMAIL needs the viewer (it splits on commas),
--                  or the frontend signs them right back out.
--   3. Backend  -> READONLY_EMAILS should have the viewer so the API blocks
--                  any changes. RLS already stops them from changing MY cards,
--                  but without this they could still add cards of their own.
--
-- Check it worked (as the viewer, after they sign in once):
--     select count(*) from cards;              -- should be my count
--     insert into cards (player, category) values ('x','other');  -- should fail
-- ============================================================
