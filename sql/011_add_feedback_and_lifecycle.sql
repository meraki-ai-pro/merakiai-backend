-- Consolidated feedback + student lifecycle.
--
-- Feedback today is spread across session_surveys, mode_feedback and
-- user_feedback, each with its own shape. That is three queries and three
-- schemas for what a study analyses as one variable, and it has no NPS, no
-- exit survey and no lecturer form at all.
--
-- Lifecycle: the platform can create a student but not remove one. Consent,
-- soft-delete, a retention window and a hard-delete are all required by the
-- pilot's ethics position (Proposal §7.2, Roadmap Part D), and none exists.
--
-- Ref: AI_Teaching_System_Technical_Specification_v3 §3, §4, §6.5
--      Meraki_AI_Integration_Roadmap Part D
--
-- Idempotent: safe to run more than once.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. feedback_responses
-- ---------------------------------------------------------------------------
-- Replaces nothing. The three existing tables keep their rows; new feedback
-- lands here. Migrating historical rows would mean guessing at a mapping for
-- data whose collection context is gone.

CREATE TABLE IF NOT EXISTS public.feedback_responses (
  id                uuid        NOT NULL DEFAULT gen_random_uuid(),
  user_id           uuid        REFERENCES public.users(id) ON DELETE SET NULL,
  -- Captured at submission time. A lecturer's rating means something different
  -- from a student's, and roles change.
  role_at_time      text,
  course_id         text        REFERENCES public.courses(id) ON DELETE SET NULL,
  session_id        uuid,
  feedback_type     text        NOT NULL
                                CHECK (feedback_type = ANY (ARRAY[
                                  'micro','nps','mode','lecturer','exit'
                                ])),
  rating            integer     CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5)),
  -- NPS is 0-10 by definition and is NOT the same scale as rating. Collapsing
  -- them would silently corrupt the Net Promoter calculation.
  nps_score         integer     CHECK (nps_score IS NULL OR (nps_score >= 0 AND nps_score <= 10)),
  mode              text,
  free_text         text,
  structured_answers jsonb      NOT NULL DEFAULT '{}'::jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT feedback_responses_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS feedback_course_idx ON public.feedback_responses (course_id, feedback_type);
CREATE INDEX IF NOT EXISTS feedback_user_idx   ON public.feedback_responses (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS feedback_type_idx   ON public.feedback_responses (feedback_type, created_at DESC);

ALTER TABLE public.feedback_responses ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS feedback_insert_own ON public.feedback_responses;
CREATE POLICY feedback_insert_own ON public.feedback_responses
  FOR INSERT WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS feedback_select_own ON public.feedback_responses;
CREATE POLICY feedback_select_own ON public.feedback_responses
  FOR SELECT USING (user_id = auth.uid());

-- Lecturers read feedback for their own courses, but there is deliberately no
-- UPDATE or DELETE policy for anyone: feedback a course owner can edit is not
-- feedback.
DROP POLICY IF EXISTS feedback_lecturer_select ON public.feedback_responses;
CREATE POLICY feedback_lecturer_select ON public.feedback_responses
  FOR SELECT USING (public.owns_course(course_id));

DROP POLICY IF EXISTS feedback_admin_select ON public.feedback_responses;
CREATE POLICY feedback_admin_select ON public.feedback_responses
  FOR SELECT USING (public.is_admin());


-- ---------------------------------------------------------------------------
-- 2. Lifecycle columns on users
-- ---------------------------------------------------------------------------

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS research_consent boolean NOT NULL DEFAULT false;

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS research_consent_at timestamptz;

-- Soft-delete. Set at request; the row survives until the retention window
-- expires so an accidental or coerced deletion can be reversed.
ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS deletion_requested_at timestamptz;

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS purge_after timestamptz;

CREATE INDEX IF NOT EXISTS users_purge_idx ON public.users (purge_after)
  WHERE deleted_at IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 3. Deletion audit
-- ---------------------------------------------------------------------------
-- Survives the user it describes: this is the record that a deletion happened
-- and was honoured, so it cannot be a row inside the data being deleted.

CREATE TABLE IF NOT EXISTS public.deletion_records (
  id                 uuid        NOT NULL DEFAULT gen_random_uuid(),
  -- Text, not a FK: the user row is gone by the time this matters.
  subject_user_id    text        NOT NULL,
  subject_email_hash text,
  requested_by       uuid        REFERENCES public.users(id) ON DELETE SET NULL,
  reason             text,
  research_consent   boolean,
  soft_deleted_at    timestamptz,
  purged_at          timestamptz,
  rows_purged        jsonb       NOT NULL DEFAULT '{}'::jsonb,
  storage_purged     integer,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT deletion_records_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS deletion_records_subject_idx
  ON public.deletion_records (subject_user_id);

ALTER TABLE public.deletion_records ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS deletion_records_admin_select ON public.deletion_records;
CREATE POLICY deletion_records_admin_select ON public.deletion_records
  FOR SELECT USING (public.is_admin());


-- ---------------------------------------------------------------------------
-- 4. Anonymisation helper
-- ---------------------------------------------------------------------------
-- Roadmap Part D: "If research_consent was true, keep only anonymised
-- aggregates / event rows without PII."
--
-- Detaching rather than deleting is the point. events.user_id is already
-- ON DELETE SET NULL, so nulling it here keeps the row countable in an
-- aggregate while making it unattributable to a person.

CREATE OR REPLACE FUNCTION public.anonymise_user_events(p_user_id uuid)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  affected integer;
BEGIN
  UPDATE public.events
     SET user_id = NULL,
         payload = payload - 'email' - 'name' - 'student_id'
   WHERE user_id = p_user_id;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RETURN affected;
END;
$$;

COMMIT;


-- Verify:
--   SELECT feedback_type, count(*) FROM public.feedback_responses GROUP BY 1;
--   SELECT id, email, research_consent, deleted_at, purge_after
--   FROM public.users WHERE deleted_at IS NOT NULL;
--   SELECT * FROM public.deletion_records ORDER BY created_at DESC LIMIT 10;
