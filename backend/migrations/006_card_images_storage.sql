-- ============================================================
-- 006_card_images_storage.sql
-- Dreamboat Slabs — private Supabase Storage bucket for card photos, with
-- owner-scoped RLS matching the policy model already used on the 6 tables.
--
-- WHY THIS EXISTS (2026-08-31):
--   `POST /scan` is in-memory by design: it runs YOLO -> GPT-4o, returns the
--   parsed fields, and discards the photos. Nothing was ever persisted, so
--   `cards.image_url` was always null and every inventory tile rendered blank.
--   This is open problem (1) in the project notes.
--
-- PATH CONVENTION
--   {user_id}/{card_id}/front.jpg
--
--   The leading segment is the owner's uid, which is what every policy below
--   checks via storage.foldername(name)[1]. Nesting by card_id means adding
--   back images later is purely additive — ".../back.jpg" needs no new policy
--   and no restructuring. (Decision 2026-08-31: option A, front image only,
--   single `cards.image_url` column, no schema migration. The path is shaped
--   so that choice is reversible.)
--
-- WHY PRIVATE + SIGNED URLS, NOT A PUBLIC BUCKET
--   A public bucket would be simpler, but paths are predictable and a public
--   bucket makes every card photo world-readable to anyone who can guess or
--   is handed a URL. This project is built multi-tenant from day one and will
--   hold other sellers' inventory, so storage gets the same owner-isolation
--   the tables already have. Clients mint short-lived signed URLs at render
--   time instead.
--
--   Corollary, and the reason `cards.image_url` stores a PATH and not a URL:
--   a signed URL expires. Persisting one would leave rows pointing at dead
--   links. The column holds the object path; the URL is derived on read.
--
-- NOTE ON RLS
--   `storage.objects` already has RLS enabled by Supabase — this migration
--   only adds policies. Policies are dropped first so the file is idempotent
--   and safe to re-run (CREATE POLICY has no IF NOT EXISTS).
-- ============================================================

-- ------------------------------------------------------------
-- 1. The bucket. `public = false` is the whole point.
-- ------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('card-images', 'card-images', false)
on conflict (id) do nothing;

-- ------------------------------------------------------------
-- 2. Owner-scoped policies.
--
--    (select auth.uid()) rather than a bare auth.uid() is deliberate: the
--    subquery form is evaluated once per statement instead of once per row,
--    which is the documented Supabase pattern for RLS performance.
-- ------------------------------------------------------------

drop policy if exists "card_images_select_own" on storage.objects;
create policy "card_images_select_own"
  on storage.objects
  for select
  to authenticated
  using (
    bucket_id = 'card-images'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

drop policy if exists "card_images_insert_own" on storage.objects;
create policy "card_images_insert_own"
  on storage.objects
  for insert
  to authenticated
  with check (
    bucket_id = 'card-images'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

-- UPDATE needs both USING (which rows may be targeted) and WITH CHECK (what
-- the row may become). Without WITH CHECK, a user could move their own object
-- into another user's folder.
drop policy if exists "card_images_update_own" on storage.objects;
create policy "card_images_update_own"
  on storage.objects
  for update
  to authenticated
  using (
    bucket_id = 'card-images'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  )
  with check (
    bucket_id = 'card-images'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

drop policy if exists "card_images_delete_own" on storage.objects;
create policy "card_images_delete_own"
  on storage.objects
  for delete
  to authenticated
  using (
    bucket_id = 'card-images'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

-- ============================================================
-- VERIFY (run after applying):
--
--   select id, public from storage.buckets where id = 'card-images';
--     -> expect one row, public = false
--
--   select policyname, cmd
--   from pg_policies
--   where schemaname = 'storage' and tablename = 'objects'
--     and policyname like 'card_images_%'
--   order by policyname;
--     -> expect 4 rows: delete, insert, select, update
-- ============================================================
