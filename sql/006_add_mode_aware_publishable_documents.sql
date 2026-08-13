-- Mode-aware, publishable knowledge files.
--
-- Two gaps this closes:
--
--   1. A document today serves exactly one mode (documents.default_mode). The
--      lecturer spec requires a multi-select — one file of worked examples is
--      legitimately both Learn material and Review material, and forcing a
--      second upload to achieve that duplicates vectors and costs twice.
--
--   2. There is no draft state. Everything is live to students the instant
--      ingestion finishes, so a lecturer cannot stage a file, run a test query
--      against it, and only then release it. Permission Checks §3.4 makes
--      "published" a retrieval filter, not a UI affordance.
--
-- Ref: Meraki_AI_Lecturer_Side_Technical_Documentation §3.2, §4.2
--      Meraki_AI_Student_Permission_Checks §3.4
--
-- Idempotent: safe to run more than once.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Columns
-- ---------------------------------------------------------------------------

ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS target_modes text[];

-- Defaults to TRUE, not FALSE. Every existing document is already serving
-- students; defaulting to draft would silently empty every course's knowledge
-- base the moment the retrieval filter goes live.
ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS is_published boolean NOT NULL DEFAULT true;

ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS topic text;

ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS previous_version_id uuid REFERENCES public.documents(id) ON DELETE SET NULL;

ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

-- Where the original upload lives in Supabase Storage. Ingestion currently
-- discards the bytes after parsing, which makes re-ingestion and file
-- versioning impossible without the lecturer re-uploading (task #14).
ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS storage_path text;


-- ---------------------------------------------------------------------------
-- 2. Backfill
-- ---------------------------------------------------------------------------
-- Every existing document serves exactly the mode it was ingested for.

UPDATE public.documents
   SET target_modes = ARRAY[default_mode]
 WHERE target_modes IS NULL;

ALTER TABLE public.documents
  ALTER COLUMN target_modes SET DEFAULT '{}';

-- Applied after the backfill so no existing NULL row blocks the migration.
ALTER TABLE public.documents
  DROP CONSTRAINT IF EXISTS documents_target_modes_check;

ALTER TABLE public.documents
  ADD CONSTRAINT documents_target_modes_check
  CHECK (
    target_modes IS NULL
    OR target_modes <@ ARRAY['learn'::text, 'review'::text, 'application'::text]
  );


-- ---------------------------------------------------------------------------
-- 3. Indexes
-- ---------------------------------------------------------------------------
-- The retriever asks "which documents may this student see for this course and
-- mode?" on every turn, so that lookup must not scan.

CREATE INDEX IF NOT EXISTS documents_visibility_idx
  ON public.documents (course_id, is_published)
  WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS documents_target_modes_idx
  ON public.documents USING gin (target_modes);


-- ---------------------------------------------------------------------------
-- 4. Visibility helper
-- ---------------------------------------------------------------------------
-- Returns the document ids a student may retrieve from. Kept in SQL so the
-- retriever and any future lecturer preview share one definition of
-- "visible" — two implementations would drift and one of them would leak.

CREATE OR REPLACE FUNCTION public.visible_document_ids(
  p_course_id text,
  p_mode      text
)
RETURNS TABLE (id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT d.id
  FROM public.documents d
  WHERE d.course_id = p_course_id
    AND d.is_published
    AND d.deleted_at IS NULL
    AND d.status = 'ready'
    AND (d.target_modes IS NULL OR p_mode = ANY (d.target_modes));
$$;

COMMIT;


-- Verify:
--   SELECT id, title, default_mode, target_modes, is_published, status
--   FROM public.documents ORDER BY created_at;
--
--   SELECT * FROM public.visible_document_ids('froth-flotation', 'learn');
