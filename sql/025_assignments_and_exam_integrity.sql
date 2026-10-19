-- 025: Assigned activities and exam integrity events (after 016 and 017).
--
-- 1. student_assignments — what the Intervention Studio gave each student: a
--    mini-lesson (a session) or a practice paper (an assessment). The student's
--    Activities tab lists them. Completion is not stored: a lesson is done when
--    the student has replied in it, a paper when it has been submitted, so the
--    two can never disagree. `required` = the student must finish it before
--    starting or reopening other sessions on that course.
--
-- 2. assessment_starts.integrity_events — what happened during a sitting that a
--    lecturer should know about: leaving the exam window, exiting full screen,
--    pasting. Recorded and shown with the marking; nothing is auto-penalised.
--
-- Both are read and written only by the API's service role, like
-- assessment_starts itself. Idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS public.student_assignments (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  course_id     text        NOT NULL REFERENCES public.courses(id) ON DELETE CASCADE,
  student_id    uuid        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  kind          text        NOT NULL CHECK (kind IN ('lesson', 'paper')),
  session_id    uuid        REFERENCES public.sessions(id) ON DELETE CASCADE,
  assessment_id uuid        REFERENCES public.assessments(id) ON DELETE CASCADE,
  title         text        NOT NULL,
  required      boolean     NOT NULL DEFAULT true,
  assigned_by   uuid        REFERENCES public.users(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'lesson') = (session_id IS NOT NULL)),
  CHECK ((kind = 'paper') = (assessment_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS student_assignments_student_course_idx
  ON public.student_assignments (student_id, course_id);

ALTER TABLE public.student_assignments ENABLE ROW LEVEL SECURITY;
-- Written and read only through the service role by the API; no client policy.

ALTER TABLE public.assessment_starts
  ADD COLUMN IF NOT EXISTS integrity_events jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMIT;
