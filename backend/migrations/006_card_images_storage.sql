-- ============================================================
-- 006_card_images_storage.sql
-- A private storage bucket for card photos, where each user can only get to
-- their own photos, same as the tables.
--
-- /scan reads the card and sends back the fields but doesn't keep the photos.
-- So cards.image_url was always empty and every card in my inventory had no
-- picture.
--
-- Where photos go:
--   {user_id}/{card_id}/front.jpg
--
--   The first folder is the owner's id, which is what all the policies below
--   check with storage.foldername(name)[1]. Putting each card in its own
--   folder means I can add back photos later (".../back.jpg") without changing
--   any policies. For now it's front only with the one image_url column.
--
-- Why private with signed links instead of a public bucket:
--   A public bucket is easier, but the paths are easy to guess, so anyone could
--   see anyone's card photos. The app is built for other sellers to use
--   eventually, so photos get the same protection as the tables. The frontend
--   makes a temporary signed link when it shows a photo.
--
--   That's also why image_url saves a path and not a link. Signed links expire,
--   so a saved one would just break later.
--
-- RLS is already on for storage.objects in Supabase, this just adds the
-- policies. They get dropped first so it's safe to run again (CREATE POLICY
-- doesn't have IF NOT EXISTS).
-- ============================================================

-- ------------------------------------------------------------
-- 1. The bucket. public = false is the important part.
-- ------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('card-images', 'card-images', false)
on conflict (id) do nothing;

-- ------------------------------------------------------------
-- 2. Policies so you can only get to your own photos.
--
--    (select auth.uid()) instead of just auth.uid() on purpose. It only runs
--    once per query instead of once per row, which is what Supabase recommends
--    for speed.
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

-- UPDATE needs USING (which files you can change) and WITH CHECK (what they
-- can be changed to). Without WITH CHECK, someone could move their own file
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
-- Check it worked (run after applying):
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
