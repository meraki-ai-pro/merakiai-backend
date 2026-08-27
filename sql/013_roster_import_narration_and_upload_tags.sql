-- Client-requested lecturer/admin/student changes, August 2026.
--
-- Six independent additions, kept in one file because they ship together:
--
--   1. Roster import. A lecturer uploads a spreadsheet of names and emails;
--      accounts that already exist are enrolled immediately, and the rest are
--      held as pending invitations that convert the moment the student signs
--      up. Without the pending half, importing a class list before the cohort
--      has registered silently does nothing — which is the normal case at the
--      start of a semester.
--
--   2. Upload tagging. Review material is tagged with the question formats it
--      can support; scenario (application) material carries a difficulty. Both
--      steer retrieval as a PREFERENCE, never a hard filter — see
--      app/ai/rag/visibility.py.
--
--   3. Narrated concept videos. The rendered mp4 now carries a spoken track,
--      so the script that produced it is worth keeping next to the asset.
--
--   4. Video revisions. A lecturer who rejects a video edits the prompt and
--      re-renders; the new asset points back at the one it replaces.
--
--   5. Attributable feedback. Free-text feedback now records which course the
--      student was studying, so the admin inbox can show it as a course
--      problem rather than an orphaned sentence.
--
--   6. courses.subject. The renderer router already consults subject as a
--      fallback (app/media/render/routing.py) but nothing ever supplied one —
--      the UI hard-coded "mathematics", so a Biology course was routed to
--      Manim.
--
-- PREREQUISITES: 003 (owns_course/is_admin), 004 (enrolments, touch_updated_at),
--                006 (documents.target_modes), 008 (media_assets).
--
-- Idempotent: safe to run more than once.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Roster import: pending enrolment invitations
-- ---------------------------------------------------------------------------
-- Email is stored already lowercased by the importer, and the unique key is a
-- plain (course_id, email) constraint rather than a unique index on
-- lower(email): PostgREST can only infer an ON CONFLICT target from real
-- columns, and the upsert is what makes re-importing a corrected spreadsheet
-- update the held rows instead of failing the whole file.

CREATE TABLE IF NOT EXISTS public.enrolment_invitations (
  id           uuid        NOT NULL DEFAULT gen_random_uuid(),
  course_id    text        NOT NULL REFERENCES public.courses(id) ON DELETE CASCADE,
  email        text        NOT NULL,
  first_name   text,
  last_name    text,
  status       text        NOT NULL DEFAULT 'pending'
                           CHECK (status = ANY (ARRAY[
                             'pending'::text,
                             'accepted'::text,
                             'cancelled'::text
                           ])),
  invited_by   uuid        REFERENCES public.users(id) ON DELETE SET NULL,
  accepted_at  timestamptz,
  -- Informational, and deliberately NOT a foreign key. It is written the
  -- instant an account is created, and on the OAuth path public.users is
  -- populated by a trigger whose ordering we do not control — an FK violation
  -- there would abort the acceptance and silently leave an imported student
  -- off their course.
  accepted_by  uuid,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT enrolment_invitations_pkey PRIMARY KEY (id),
  CONSTRAINT enrolment_invitations_course_email_key UNIQUE (course_id, email)
);

-- The signup-time lookup: "does this new account have anything waiting?"
CREATE INDEX IF NOT EXISTS enrolment_invitations_pending_idx
  ON public.enrolment_invitations (email)
  WHERE status = 'pending';

DROP TRIGGER IF EXISTS enrolment_invitations_touch_updated_at ON public.enrolment_invitations;
CREATE TRIGGER enrolment_invitations_touch_updated_at
  BEFORE UPDATE ON public.enrolment_invitations
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- The unique constraint is on the raw column, so "Ama@ug.edu.gh" and
-- "ama@ug.edu.gh" would otherwise be two invitations for one student and the
-- second would never be accepted. Normalised here rather than trusting every
-- caller to remember.
CREATE OR REPLACE FUNCTION public.normalise_invitation_email()
RETURNS trigger
LANGUAGE plpgsql
AS $norm$
BEGIN
  NEW.email := lower(btrim(NEW.email));
  RETURN NEW;
END;
$norm$;

DROP TRIGGER IF EXISTS enrolment_invitations_normalise_email ON public.enrolment_invitations;
CREATE TRIGGER enrolment_invitations_normalise_email
  BEFORE INSERT OR UPDATE ON public.enrolment_invitations
  FOR EACH ROW EXECUTE FUNCTION public.normalise_invitation_email();

ALTER TABLE public.enrolment_invitations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS enrolment_invitations_lecturer_all ON public.enrolment_invitations;
CREATE POLICY enrolment_invitations_lecturer_all ON public.enrolment_invitations
  FOR ALL
  USING (public.owns_course(course_id))
  WITH CHECK (public.owns_course(course_id));

DROP POLICY IF EXISTS enrolment_invitations_admin_all ON public.enrolment_invitations;
CREATE POLICY enrolment_invitations_admin_all ON public.enrolment_invitations
  FOR ALL USING (public.is_admin()) WITH CHECK (public.is_admin());

-- Students may see (only) their own pending invitations, so the course picker
-- can offer "your lecturer has added you to Calculus".
DROP POLICY IF EXISTS enrolment_invitations_own_select ON public.enrolment_invitations;
CREATE POLICY enrolment_invitations_own_select ON public.enrolment_invitations
  FOR SELECT
  USING (
    lower(email) = lower(COALESCE(
      (SELECT u.email FROM public.users u WHERE u.id = auth.uid()), ''
    ))
  );


-- Converts every pending invitation for one email into a live enrolment.
-- SECURITY DEFINER because it runs for a brand-new account that owns nothing
-- yet, and because the signup path calls it before the user has a session.
CREATE OR REPLACE FUNCTION public.accept_enrolment_invitations(
  p_user_id uuid,
  p_email   text
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
  v_invite public.enrolment_invitations;
  v_count  integer := 0;
BEGIN
  IF p_user_id IS NULL OR btrim(COALESCE(p_email, '')) = '' THEN
    RETURN 0;
  END IF;

  FOR v_invite IN
    SELECT * FROM public.enrolment_invitations
     WHERE lower(email) = lower(btrim(p_email))
       AND status = 'pending'
     FOR UPDATE
  LOOP
    -- A student the lecturer has since withdrawn must not be silently
    -- reinstated by an old spreadsheet row, so a withdrawn enrolment is left
    -- exactly as it is.
    -- `enrolments.status`, not `public.enrolments.status`: inside ON CONFLICT
    -- DO UPDATE the existing row is addressed by the table's own name, and the
    -- schema-qualified form is a syntax error.
    INSERT INTO public.enrolments (course_id, student_id, status)
    VALUES (v_invite.course_id, p_user_id, 'active')
    ON CONFLICT (course_id, student_id) DO UPDATE
       SET status     = CASE WHEN enrolments.status = 'withdrawn'
                             THEN 'withdrawn' ELSE 'active' END,
           updated_at = now();

    UPDATE public.enrolment_invitations
       SET status = 'accepted', accepted_at = now(), accepted_by = p_user_id
     WHERE id = v_invite.id;

    v_count := v_count + 1;
  END LOOP;

  RETURN v_count;
END;
$fn$;

REVOKE ALL ON FUNCTION public.accept_enrolment_invitations(uuid, text) FROM public;
GRANT EXECUTE ON FUNCTION public.accept_enrolment_invitations(uuid, text) TO service_role;


-- ---------------------------------------------------------------------------
-- 2. Upload tagging
-- ---------------------------------------------------------------------------
-- 'flashcard' is deliberately absent: the client removed it from the student
-- question-format picker, so material can no longer be tagged for it either.

ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS question_formats text[];

ALTER TABLE public.documents
  DROP CONSTRAINT IF EXISTS documents_question_formats_check;

ALTER TABLE public.documents
  ADD CONSTRAINT documents_question_formats_check
  CHECK (
    question_formats IS NULL
    OR question_formats <@ ARRAY['mcq'::text, 'fill_blank'::text, 'short_answer'::text]
  );

CREATE INDEX IF NOT EXISTS documents_question_formats_idx
  ON public.documents USING gin (question_formats);


-- ---------------------------------------------------------------------------
-- 3 & 4. Narration and revisions on rendered media
-- ---------------------------------------------------------------------------

ALTER TABLE public.media_assets
  ADD COLUMN IF NOT EXISTS narration_script text;

ALTER TABLE public.media_assets
  ADD COLUMN IF NOT EXISTS has_audio boolean NOT NULL DEFAULT false;

-- Narration is a SECOND job on a different worker, not part of the render.
-- The render container runs model-generated code and deliberately carries no
-- ElevenLabs client and no key for one (render.Dockerfile), so the spoken
-- track is added afterwards by the media worker. That makes "rendered but not
-- yet narrated" a real state the lecturer's review queue has to show, rather
-- than something to infer from a null.
--
-- 'skipped' covers narration being switched off, or an asset type that has
-- nothing to read aloud.
ALTER TABLE public.media_assets
  ADD COLUMN IF NOT EXISTS narration_status text NOT NULL DEFAULT 'pending';

ALTER TABLE public.media_assets
  DROP CONSTRAINT IF EXISTS media_assets_narration_status_check;

ALTER TABLE public.media_assets
  ADD CONSTRAINT media_assets_narration_status_check
  CHECK (narration_status = ANY (ARRAY[
    'pending'::text, 'narrating'::text, 'ready'::text, 'failed'::text, 'skipped'::text
  ]));

-- Every asset that predates narration has no spoken track and never will
-- unless it is re-rendered; leaving them 'pending' would park them in the
-- lecturer's "adding audio" state for ever.
UPDATE public.media_assets
   SET narration_status = 'skipped'
 WHERE narration_status = 'pending'
   AND created_at < now();

-- Which asset this one supersedes, and what the lecturer changed. Kept so the
-- review queue can show a video as revision 3 of a concept rather than as an
-- unexplained fourth row.
ALTER TABLE public.media_assets
  ADD COLUMN IF NOT EXISTS parent_asset_id uuid
  REFERENCES public.media_assets(id) ON DELETE SET NULL;

ALTER TABLE public.media_assets
  ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1;

ALTER TABLE public.media_assets
  ADD COLUMN IF NOT EXISTS revision_note text;

CREATE INDEX IF NOT EXISTS media_assets_parent_idx
  ON public.media_assets (parent_asset_id)
  WHERE parent_asset_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 5. Attributable free-text feedback
-- ---------------------------------------------------------------------------
-- user_feedback records who said something and what type it was, but not what
-- they were studying. "The derivative in step 3 is wrong" is unactionable
-- without the course, and the admin inbox could only ever show it as an
-- orphaned sentence. session_id is present but nullable and most feedback is
-- sent outside a session, so it cannot stand in for this.

ALTER TABLE public.user_feedback
  ADD COLUMN IF NOT EXISTS course_id text REFERENCES public.courses(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS user_feedback_course_idx
  ON public.user_feedback (course_id, created_at DESC);


-- ---------------------------------------------------------------------------
-- 6. courses.subject
-- ---------------------------------------------------------------------------
-- Free text on purpose. It is matched loosely against SUBJECT_DEFAULTS in
-- app/media/render/routing.py, which already handles "BSc Mathematics" and
-- "Quantitative Techniques II"; a closed enum would reject the course names
-- Ghanaian departments actually use.

ALTER TABLE public.courses
  ADD COLUMN IF NOT EXISTS subject text;

COMMIT;


-- Verify:
--   SELECT column_name FROM information_schema.columns
--    WHERE table_name = 'enrolment_invitations' ORDER BY ordinal_position;
--
--   SELECT question_formats FROM public.documents LIMIT 5;
--   SELECT has_audio, revision, parent_asset_id FROM public.media_assets LIMIT 5;
--   SELECT id, subject FROM public.courses;
