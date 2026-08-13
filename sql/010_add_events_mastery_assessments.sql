-- Research instrumentation: events, mastery, pre/post assessments.
--
-- This is what turns the pilot from a demo into a study. Today the platform
-- can show that students used it; it cannot show that they learned anything,
-- because there is no baseline, no post-measure and no per-topic mastery.
--
-- Three pieces:
--   events              — the analytics stream (Tech Spec §6.5)
--   mastery_states      — per-topic proficiency (§6.4)
--   assessments/-questions/-attempts — the pre/post instrument (§5.1)
--
-- Ref: AI_Teaching_System_Technical_Specification_v3 §5.1, §6.4, §6.5
--      Meraki_AI_Integration_Roadmap §B.2
--
-- Idempotent: safe to run more than once.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. events
-- ---------------------------------------------------------------------------
-- Deliberately schemaless in the payload. The event *types* are a closed set
-- enforced in app/core/events.py; the payload shape varies per type and
-- pinning it in DDL would mean a migration every time a metric is added.

CREATE TABLE IF NOT EXISTS public.events (
  id         uuid        NOT NULL DEFAULT gen_random_uuid(),
  user_id    uuid        REFERENCES public.users(id) ON DELETE SET NULL,
  event_type text        NOT NULL,
  course_id  text        REFERENCES public.courses(id) ON DELETE SET NULL,
  topic      text,
  session_id uuid,
  payload    jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT events_pkey PRIMARY KEY (id)
);

-- user_id survives account deletion as NULL so anonymised aggregates remain
-- analysable after a student exercises their right to be forgotten
-- (Roadmap Part D: "keep only anonymised aggregates ... without PII").

CREATE INDEX IF NOT EXISTS events_course_time_idx ON public.events (course_id, created_at DESC);
CREATE INDEX IF NOT EXISTS events_type_time_idx   ON public.events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS events_user_idx        ON public.events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS events_payload_idx     ON public.events USING gin (payload);

ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;

-- Students never read the stream — it is research data about them, not for
-- them. Writes go through the service role only.
DROP POLICY IF EXISTS events_lecturer_select ON public.events;
CREATE POLICY events_lecturer_select ON public.events
  FOR SELECT USING (public.owns_course(course_id));

DROP POLICY IF EXISTS events_admin_select ON public.events;
CREATE POLICY events_admin_select ON public.events
  FOR SELECT USING (public.is_admin());


-- ---------------------------------------------------------------------------
-- 2. mastery_states
-- ---------------------------------------------------------------------------
-- Topic is text, not a FK: there is no topics table, and the pilot's topics
-- come from document metadata and question tags as free text.

CREATE TABLE IF NOT EXISTS public.mastery_states (
  id               uuid        NOT NULL DEFAULT gen_random_uuid(),
  student_id       uuid        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  course_id        text        NOT NULL REFERENCES public.courses(id) ON DELETE CASCADE,
  topic            text        NOT NULL,
  mastery_score    numeric     NOT NULL DEFAULT 0
                               CHECK (mastery_score >= 0 AND mastery_score <= 1),
  attempts_count   integer     NOT NULL DEFAULT 0,
  correct_count    integer     NOT NULL DEFAULT 0,
  last_practised_at timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT mastery_states_pkey PRIMARY KEY (id),
  CONSTRAINT mastery_states_unique UNIQUE (student_id, course_id, topic)
);

CREATE INDEX IF NOT EXISTS mastery_course_idx ON public.mastery_states (course_id, topic);

ALTER TABLE public.mastery_states ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mastery_student_select ON public.mastery_states;
CREATE POLICY mastery_student_select ON public.mastery_states
  FOR SELECT USING (student_id = auth.uid());

DROP POLICY IF EXISTS mastery_lecturer_select ON public.mastery_states;
CREATE POLICY mastery_lecturer_select ON public.mastery_states
  FOR SELECT USING (public.owns_course(course_id));

DROP POLICY IF EXISTS mastery_admin_all ON public.mastery_states;
CREATE POLICY mastery_admin_all ON public.mastery_states
  FOR ALL USING (public.is_admin()) WITH CHECK (public.is_admin());

DROP TRIGGER IF EXISTS mastery_touch_updated_at ON public.mastery_states;
CREATE TRIGGER mastery_touch_updated_at
  BEFORE UPDATE ON public.mastery_states
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();


-- ---------------------------------------------------------------------------
-- 3. Pre/post assessment instrument
-- ---------------------------------------------------------------------------
-- 'kind' is what makes a learning gain computable: the same student's post
-- score minus their pre score, per topic. Without a labelled pair there is no
-- primary outcome measure and the pilot reports engagement only.

CREATE TABLE IF NOT EXISTS public.assessments (
  id           uuid        NOT NULL DEFAULT gen_random_uuid(),
  course_id    text        NOT NULL REFERENCES public.courses(id) ON DELETE CASCADE,
  kind         text        NOT NULL
                           CHECK (kind = ANY (ARRAY['pre','post','retention'])),
  title        text        NOT NULL,
  instructions text,
  is_published boolean     NOT NULL DEFAULT false,
  created_by   uuid        REFERENCES public.users(id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT assessments_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS assessments_course_idx ON public.assessments (course_id, kind);

CREATE TABLE IF NOT EXISTS public.assessment_questions (
  id             uuid        NOT NULL DEFAULT gen_random_uuid(),
  assessment_id  uuid        NOT NULL REFERENCES public.assessments(id) ON DELETE CASCADE,
  order_index    integer     NOT NULL DEFAULT 0,
  prompt         text        NOT NULL,
  -- Multiple choice keeps scoring objective, which matters for a study.
  options        jsonb       NOT NULL DEFAULT '[]'::jsonb,
  correct_answer text        NOT NULL,
  topic          text,
  points         numeric     NOT NULL DEFAULT 1 CHECK (points > 0),
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT assessment_questions_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS assessment_questions_idx
  ON public.assessment_questions (assessment_id, order_index);

CREATE TABLE IF NOT EXISTS public.assessment_attempts (
  id                 uuid        NOT NULL DEFAULT gen_random_uuid(),
  assessment_id      uuid        NOT NULL REFERENCES public.assessments(id) ON DELETE CASCADE,
  question_id        uuid        NOT NULL REFERENCES public.assessment_questions(id) ON DELETE CASCADE,
  student_id         uuid        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  course_id          text        REFERENCES public.courses(id) ON DELETE SET NULL,
  topic              text,
  student_answer     text,
  is_correct         boolean,
  score              numeric     NOT NULL DEFAULT 0,
  time_spent_seconds integer,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT assessment_attempts_pkey PRIMARY KEY (id),
  -- One answer per student per question. A retake would otherwise silently
  -- double-count and inflate the gain.
  CONSTRAINT assessment_attempts_unique UNIQUE (assessment_id, question_id, student_id)
);

CREATE INDEX IF NOT EXISTS attempts_student_idx ON public.assessment_attempts (student_id, assessment_id);
CREATE INDEX IF NOT EXISTS attempts_course_idx  ON public.assessment_attempts (course_id, topic);

ALTER TABLE public.assessments          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.assessment_questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.assessment_attempts  ENABLE ROW LEVEL SECURITY;

-- Students see published assessments on courses they are enrolled on.
DROP POLICY IF EXISTS assessments_student_select ON public.assessments;
CREATE POLICY assessments_student_select ON public.assessments
  FOR SELECT USING (is_published AND public.is_enrolled(course_id));

DROP POLICY IF EXISTS assessments_lecturer_all ON public.assessments;
CREATE POLICY assessments_lecturer_all ON public.assessments
  FOR ALL USING (public.owns_course(course_id)) WITH CHECK (public.owns_course(course_id));

-- NOTE: no student SELECT policy on assessment_questions. correct_answer lives
-- on that row, and a student who can read the table can read the answer key.
-- The student-facing endpoint strips it server-side using the service role.
DROP POLICY IF EXISTS questions_lecturer_all ON public.assessment_questions;
CREATE POLICY questions_lecturer_all ON public.assessment_questions
  FOR ALL
  USING (EXISTS (
    SELECT 1 FROM public.assessments a
    WHERE a.id = assessment_id AND public.owns_course(a.course_id)
  ))
  WITH CHECK (EXISTS (
    SELECT 1 FROM public.assessments a
    WHERE a.id = assessment_id AND public.owns_course(a.course_id)
  ));

DROP POLICY IF EXISTS attempts_student_select ON public.assessment_attempts;
CREATE POLICY attempts_student_select ON public.assessment_attempts
  FOR SELECT USING (student_id = auth.uid());

DROP POLICY IF EXISTS attempts_lecturer_select ON public.assessment_attempts;
CREATE POLICY attempts_lecturer_select ON public.assessment_attempts
  FOR SELECT USING (public.owns_course(course_id));

DROP POLICY IF EXISTS attempts_admin_all ON public.assessment_attempts;
CREATE POLICY attempts_admin_all ON public.assessment_attempts
  FOR ALL USING (public.is_admin()) WITH CHECK (public.is_admin());

DROP TRIGGER IF EXISTS assessments_touch_updated_at ON public.assessments;
CREATE TRIGGER assessments_touch_updated_at
  BEFORE UPDATE ON public.assessments
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

COMMIT;


-- Verify:
--   SELECT event_type, count(*) FROM public.events GROUP BY 1 ORDER BY 2 DESC;
--   SELECT * FROM public.mastery_states LIMIT 5;
--   SELECT kind, title, is_published FROM public.assessments;
