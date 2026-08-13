-- Renderer-agnostic media job pipeline.
--
-- Activates media_assets (Tech Spec §6.6) as the job table behind Manim and,
-- later, Remotion. One row is one rendered artefact and its whole lifecycle:
-- queued -> rendering -> ready -> approved, or failed with a reason.
--
-- The central design decision this encodes: renders are NOT part of a student
-- turn. A Manim render takes minutes; putting it where D-ID sits would rebuild
-- the exact latency problem the Lesson Board was built to avoid. These are
-- authored once, reviewed by the lecturer, cached, and then replayed instantly
-- by every student in the cohort.
--
-- Ref: AI_Teaching_System_Technical_Specification_v3 §6.6
--      AI_Teaching_System_Project_Proposal §6.2, §10
--
-- Idempotent: safe to run more than once.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. media_assets
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.media_assets (
  id            uuid        NOT NULL DEFAULT gen_random_uuid(),
  course_id     text        REFERENCES public.courses(id) ON DELETE CASCADE,
  topic         text,

  -- Stable name for the concept this explains, e.g. "chain-rule". The cache
  -- key together with content_hash.
  concept_key   text        NOT NULL,

  type          text        NOT NULL DEFAULT 'video'
                            CHECK (type = ANY (ARRAY['video','audio','image','diagram'])),

  -- Which pipeline produced it. 'did' and 'tavus' are included so the existing
  -- avatar path can be brought under the same table later without a migration.
  renderer      text        NOT NULL
                            CHECK (renderer = ANY (ARRAY['manim','remotion','did','tavus'])),

  -- What the visual DOES, which is what actually selects a renderer. Subject is
  -- only a default hint — see app/media/render/routing.py.
  archetype     text,

  -- The lesson script the artefact was generated from, and the code that was
  -- executed. scene_code is retained for lecturer review and for reproducing a
  -- render without re-invoking the model.
  source_script text,
  scene_code    text,

  status        text        NOT NULL DEFAULT 'queued'
                            CHECK (status = ANY (ARRAY['queued','rendering','ready','failed'])),
  error         text,

  storage_path  text,
  duration_seconds numeric,

  -- sha256 of the source script. Together with (course_id, renderer,
  -- concept_key) this makes a re-request of unchanged content a cache hit
  -- rather than another multi-minute render.
  content_hash  text        NOT NULL,

  created_by    uuid        REFERENCES public.users(id) ON DELETE SET NULL,

  -- Proposal §10 names notation errors as a named risk and lecturer review of
  -- every video as the mitigation. Students only ever see approved rows.
  approved_by   uuid        REFERENCES public.users(id) ON DELETE SET NULL,
  approved_at   timestamptz,
  rejected_at   timestamptz,
  review_note   text,

  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz,

  CONSTRAINT media_assets_pkey PRIMARY KEY (id)
);

-- The cache key. Re-requesting an unchanged concept must find the existing row
-- instead of queueing a duplicate render.
CREATE UNIQUE INDEX IF NOT EXISTS media_assets_cache_key
  ON public.media_assets (course_id, renderer, concept_key, content_hash);

-- The student-facing lookup: approved, ready assets for a course.
CREATE INDEX IF NOT EXISTS media_assets_playable_idx
  ON public.media_assets (course_id, concept_key)
  WHERE status = 'ready' AND approved_at IS NOT NULL;

-- The lecturer review queue.
CREATE INDEX IF NOT EXISTS media_assets_review_idx
  ON public.media_assets (course_id, status)
  WHERE approved_at IS NULL AND rejected_at IS NULL;

DROP TRIGGER IF EXISTS media_assets_touch_updated_at ON public.media_assets;
CREATE TRIGGER media_assets_touch_updated_at
  BEFORE UPDATE ON public.media_assets
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();


-- ---------------------------------------------------------------------------
-- 2. RLS
-- ---------------------------------------------------------------------------

ALTER TABLE public.media_assets ENABLE ROW LEVEL SECURITY;

-- Students see approved, ready assets on courses they are enrolled on, and
-- nothing else. An unapproved render is invisible: that is the whole point of
-- the review gate, and a draft animation with a wrong sign must not be
-- reachable by guessing an id.
DROP POLICY IF EXISTS media_assets_student_select ON public.media_assets;
CREATE POLICY media_assets_student_select ON public.media_assets
  FOR SELECT
  USING (
    status = 'ready'
    AND approved_at IS NOT NULL
    AND public.is_enrolled(course_id)
  );

DROP POLICY IF EXISTS media_assets_lecturer_all ON public.media_assets;
CREATE POLICY media_assets_lecturer_all ON public.media_assets
  FOR ALL
  USING (public.owns_course(course_id))
  WITH CHECK (public.owns_course(course_id));

DROP POLICY IF EXISTS media_assets_admin_all ON public.media_assets;
CREATE POLICY media_assets_admin_all ON public.media_assets
  FOR ALL USING (public.is_admin()) WITH CHECK (public.is_admin());


-- ---------------------------------------------------------------------------
-- 3. Storage
-- ---------------------------------------------------------------------------
-- Private, like course-documents. These are derived teaching materials built
-- from the lecturer's own notes; they are shown to enrolled students through
-- signed URLs, not published to the open web.

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
  'rendered-media',
  'rendered-media',
  false,
  209715200,  -- 200 MB; a 3-minute 1080p Manim render lands far below this
  ARRAY['video/mp4', 'image/png', 'image/svg+xml', 'text/vtt']
)
ON CONFLICT (id) DO NOTHING;

COMMIT;


-- Verify:
--   SELECT column_name, data_type FROM information_schema.columns
--   WHERE table_name = 'media_assets' ORDER BY ordinal_position;
--
--   SELECT polname FROM pg_policy WHERE polrelid = 'public.media_assets'::regclass;
--
--   SELECT id, public, file_size_limit FROM storage.buckets WHERE id = 'rendered-media';
