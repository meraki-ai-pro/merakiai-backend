-- 018: Pin and archive sessions (client request, 2026-10-14).
--
-- Students asked to keep important sessions at the top of the sidebar and to
-- tidy finished ones away without deleting them. Both are timestamps rather
-- than booleans: "pinned 3 days ago" orders the pinned list, and NULL means
-- "not pinned / not archived" for every existing row, so no backfill.
--
-- Archiving clears the pin (the API does this), so a session is never both.
-- RLS is unchanged: the existing sessions policies already let a user update
-- only their own rows, and these columns ride on that.
--
-- Requires 016 (sessions.title sits beside these). Idempotent.

BEGIN;

ALTER TABLE public.sessions ADD COLUMN IF NOT EXISTS pinned_at   timestamptz;
ALTER TABLE public.sessions ADD COLUMN IF NOT EXISTS archived_at timestamptz;

-- The sidebar asks for one user's non-archived sessions on every load.
CREATE INDEX IF NOT EXISTS sessions_user_archived_idx
  ON public.sessions (user_id, archived_at, started_at DESC);

COMMIT;

-- Verify:
--   SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'sessions' AND column_name IN ('pinned_at', 'archived_at');
