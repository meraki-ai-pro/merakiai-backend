-- 028: Course engagement survives deletion of chat sessions.
--
-- A session is user-owned history and may be permanently deleted.  "This
-- student has ever started the course" is a separate, durable fact used by
-- lecturer analytics and must not disappear with the final chat transcript.

BEGIN;

ALTER TABLE public.enrolments
  ADD COLUMN IF NOT EXISTS first_started_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_activity_at timestamptz;

-- Recover the earliest/latest evidence we still have.  Events deliberately
-- survive session deletion (their session_id is nulled), so students who used
-- a course and later deleted every chat can still be recovered.
WITH evidence AS (
  SELECT user_id AS student_id, course_id, started_at AS happened_at
    FROM public.sessions
   WHERE user_id IS NOT NULL AND course_id IS NOT NULL
  UNION ALL
  SELECT user_id AS student_id, course_id, created_at AS happened_at
    FROM public.events
   WHERE user_id IS NOT NULL
     AND course_id IS NOT NULL
     AND event_type IN (
       'session.started', 'turn.completed', 'mastery.updated',
       'assessment.submitted', 'misconception.detected'
     )
), activity AS (
  SELECT student_id, course_id, MIN(happened_at) AS first_at, MAX(happened_at) AS last_at
    FROM evidence
   GROUP BY student_id, course_id
)
UPDATE public.enrolments e
   SET first_started_at = CASE
         WHEN e.first_started_at IS NULL THEN a.first_at
         ELSE LEAST(e.first_started_at, a.first_at)
       END,
       last_activity_at = CASE
         WHEN e.last_activity_at IS NULL THEN a.last_at
         ELSE GREATEST(e.last_activity_at, a.last_at)
       END,
       updated_at = now()
  FROM activity a
 WHERE e.student_id = a.student_id
   AND e.course_id = a.course_id;

CREATE OR REPLACE FUNCTION public.mark_enrolment_course_started()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  UPDATE public.enrolments
     SET first_started_at = COALESCE(first_started_at, NEW.started_at),
         last_activity_at = CASE
           WHEN last_activity_at IS NULL THEN NEW.started_at
           ELSE GREATEST(last_activity_at, NEW.started_at)
         END,
         updated_at = now()
   WHERE course_id = NEW.course_id
     AND student_id = NEW.user_id;
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.mark_enrolment_course_started() FROM PUBLIC;

DROP TRIGGER IF EXISTS sessions_mark_course_started ON public.sessions;
CREATE TRIGGER sessions_mark_course_started
AFTER INSERT ON public.sessions
FOR EACH ROW
EXECUTE FUNCTION public.mark_enrolment_course_started();

CREATE INDEX IF NOT EXISTS enrolments_course_started_idx
  ON public.enrolments (course_id, first_started_at, student_id);

COMMIT;

-- Verify after applying:
-- SELECT course_id, student_id, first_started_at, last_activity_at
--   FROM public.enrolments ORDER BY course_id, first_started_at NULLS LAST;
