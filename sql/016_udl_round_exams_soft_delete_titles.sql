-- 016: UDL client round — session titles, soft-deleted users, exams.
--
-- Four unrelated additions, one file because they ship together.
--
-- 1. sessions.title
--    PATCH /sessions/{id}/title has existed for months and has never worked:
--    the column did not exist, the endpoint swallowed the error, and the list
--    endpoint derived every title from the first message anyway. A NULL title
--    still means "derive it", so no backfill is needed.
--
-- 2. users.deleted_at / deleted_by
--    Super admins remove accounts without destroying them. The row, its
--    sessions, attempts and feedback all stay, so research data and audit
--    history survive; the API also bans the auth user, which is what actually
--    stops them signing in. Restoring clears both.
--
-- 3. Exams on the existing assessment tables
--    Lecturer-set tests / mid-sems / finals / quizzes are the same shape as the
--    pre/post instrument (a paper, its questions, one attempt per student), so
--    they extend those tables instead of a parallel set. The new parts are what
--    an exam needs and a research instrument did not: a time limit, an open
--    window, short-answer questions that a person marks, and results that are
--    held back until the lecturer releases them.
--
-- 4. media_assets.beats
--    What is on screen when, recorded by the renderer. Narration is written
--    and placed per beat so the voice stays on the step being shown; without
--    it the voice was one track sized only to the total length, and drifted.
--
-- Requires 004 (enrolments), 008 (media_assets) and 010 (assessments). Idempotent.

BEGIN;

-- 1. Session titles --------------------------------------------------------

ALTER TABLE public.sessions ADD COLUMN IF NOT EXISTS title text;

-- 2. Soft delete ------------------------------------------------------------

ALTER TABLE public.users ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS deleted_by uuid;
-- deleted_by is deliberately not an FK: the admin who did it may be removed
-- later, and that must not erase the record of who removed this account.

CREATE INDEX IF NOT EXISTS users_deleted_at_idx
  ON public.users (deleted_at) WHERE deleted_at IS NOT NULL;

-- 3. Exams ------------------------------------------------------------------

ALTER TABLE public.assessments DROP CONSTRAINT IF EXISTS assessments_kind_check;
ALTER TABLE public.assessments
  ADD CONSTRAINT assessments_kind_check
  CHECK (kind = ANY (ARRAY['pre','post','retention','quiz','test','midsem','final']));

-- NULL = untimed.
ALTER TABLE public.assessments ADD COLUMN IF NOT EXISTS time_limit_minutes integer
  CHECK (time_limit_minutes IS NULL OR time_limit_minutes > 0);
-- NULL = no bound on that side.
ALTER TABLE public.assessments ADD COLUMN IF NOT EXISTS opens_at  timestamptz;
ALTER TABLE public.assessments ADD COLUMN IF NOT EXISTS closes_at timestamptz;
-- Exams hold results until the lecturer has checked the short-answer marks.
ALTER TABLE public.assessments ADD COLUMN IF NOT EXISTS results_released boolean
  NOT NULL DEFAULT false;

ALTER TABLE public.assessment_questions ADD COLUMN IF NOT EXISTS question_type text
  NOT NULL DEFAULT 'mcq';
ALTER TABLE public.assessment_questions DROP CONSTRAINT IF EXISTS assessment_questions_type_check;
ALTER TABLE public.assessment_questions
  ADD CONSTRAINT assessment_questions_type_check
  CHECK (question_type = ANY (ARRAY['mcq','fill_blank','short_answer']));
-- For short_answer, correct_answer holds the model answer / marking guide.

-- AI's suggestion is kept beside the awarded score, so an override is visible
-- as an override rather than silently replacing what the model said.
ALTER TABLE public.assessment_attempts ADD COLUMN IF NOT EXISTS needs_review boolean
  NOT NULL DEFAULT false;
ALTER TABLE public.assessment_attempts ADD COLUMN IF NOT EXISTS ai_score numeric;
ALTER TABLE public.assessment_attempts ADD COLUMN IF NOT EXISTS ai_feedback text;
ALTER TABLE public.assessment_attempts ADD COLUMN IF NOT EXISTS reviewed_by uuid;
ALTER TABLE public.assessment_attempts ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;

-- When a student opened a timed paper. The server enforces the deadline from
-- this row, never from a clock the browser reports.
CREATE TABLE IF NOT EXISTS public.assessment_starts (
  assessment_id uuid        NOT NULL REFERENCES public.assessments(id) ON DELETE CASCADE,
  student_id    uuid        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  started_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT assessment_starts_pkey PRIMARY KEY (assessment_id, student_id)
);

ALTER TABLE public.assessment_starts ENABLE ROW LEVEL SECURITY;
-- Written and read only through the service role by the API; no client policy.

-- UDL: extended time is a per-student, per-course accommodation (a student
-- with extra time has it in every paper on that course, not paper by paper).
ALTER TABLE public.enrolments ADD COLUMN IF NOT EXISTS extra_time_percent integer
  NOT NULL DEFAULT 0 CHECK (extra_time_percent BETWEEN 0 AND 200);

-- 4. Narration beats ------------------------------------------------------

ALTER TABLE public.media_assets ADD COLUMN IF NOT EXISTS beats jsonb;

COMMIT;


-- Verify:
--   SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'sessions' AND column_name = 'title';
--   SELECT conname FROM pg_constraint WHERE conname = 'assessments_kind_check';
--   SELECT to_regclass('public.assessment_starts');
