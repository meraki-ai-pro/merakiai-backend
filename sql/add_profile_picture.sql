-- ============================================================
-- Meraki AI — Add profile_picture_url to users table
-- Run in: Supabase Dashboard → SQL Editor
-- ============================================================

-- 1. Add column (idempotent — safe to re-run)
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS profile_picture_url TEXT DEFAULT NULL;

-- 2. Comment for documentation
COMMENT ON COLUMN users.profile_picture_url IS
  'Public URL of the user profile picture stored in the user-profile-pics Supabase Storage bucket.';

-- ============================================================
-- Supabase Storage bucket setup
-- Run ONCE in SQL Editor (or via Supabase Dashboard > Storage)
-- ============================================================

-- 3. Create the bucket if it does not exist
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'user-profile-pics',
    'user-profile-pics',
    true,                          -- public bucket: URLs are permanent, no signing needed
    5242880,                       -- 5 MB per file
    ARRAY['image/jpeg','image/png','image/webp']
)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Row Level Security policies for storage.objects
-- These ensure users can only upload/replace their OWN picture
-- ============================================================

-- 4. SELECT policy — scoped to own folder only.
--
--    Why not a broad SELECT?
--    The bucket is already public — direct URLs work without any policy.
--    A broad SELECT on storage.objects lets any client call storage.list()
--    and enumerate every user's file path, leaking all user IDs.
--    Restricting to (foldername)[1] = auth.uid() means:
--      - A user can list/verify their own file         ✅
--      - No one can enumerate other users' files       ✅
--      - Public URLs still resolve for anyone          ✅  (bucket-level, not policy-level)
CREATE POLICY "Users read own profile picture"
  ON storage.objects FOR SELECT
  TO authenticated
  USING (
    bucket_id = 'user-profile-pics'
    AND (storage.foldername(name))[1] = auth.uid()::text
  );

-- 5. Allow a user to upload/replace only their own picture
--    Path pattern enforced: {user_id}/profile.*
CREATE POLICY "Users upload own profile picture"
  ON storage.objects FOR INSERT
  TO authenticated
  WITH CHECK (
    bucket_id = 'user-profile-pics'
    AND (storage.foldername(name))[1] = auth.uid()::text
  );

-- 6. Allow a user to update (overwrite) only their own picture
CREATE POLICY "Users update own profile picture"
  ON storage.objects FOR UPDATE
  TO authenticated
  USING (
    bucket_id = 'user-profile-pics'
    AND (storage.foldername(name))[1] = auth.uid()::text
  );

-- 7. Allow a user to delete only their own picture
CREATE POLICY "Users delete own profile picture"
  ON storage.objects FOR DELETE
  TO authenticated
  USING (
    bucket_id = 'user-profile-pics'
    AND (storage.foldername(name))[1] = auth.uid()::text
  );

-- ============================================================
-- Verify
-- ============================================================
SELECT column_name, data_type, column_default
  FROM information_schema.columns
 WHERE table_name = 'users'
   AND column_name = 'profile_picture_url';

SELECT id, name, public, file_size_limit, allowed_mime_types
  FROM storage.buckets
 WHERE id = 'user-profile-pics';
