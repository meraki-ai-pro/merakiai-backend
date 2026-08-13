-- Backfill enrolments for accounts that predate the enrolments table.
--
-- URGENT if the #17 permission-stack code is deployed. There are existing
-- student accounts with session history and zero enrolment rows, and
-- require_enrolment() fails closed — every one of them would get 403 on their
-- next turn. This grants them exactly the access they already had.
--
-- Derives membership from observed behaviour: if an account has held a session
-- on a course, it was on that course. Safe to re-run — the unique constraint on
-- (course_id, student_id) makes it idempotent via ON CONFLICT.
--
-- PREREQUISITE: 004_add_enrolments_and_invite_codes.sql

BEGIN;

INSERT INTO public.enrolments (course_id, student_id, status, enrolled_at)
SELECT
  s.course_id,
  s.user_id,
  'active',
  MIN(s.started_at)          -- enrolled when they first showed up
FROM public.sessions s
JOIN public.users u ON u.id = s.user_id
WHERE s.course_id IS NOT NULL
  AND u.role = 'user'        -- staff bypass the check; no row needed
GROUP BY s.course_id, s.user_id
ON CONFLICT (course_id, student_id) DO NOTHING;

COMMIT;


-- Verify — expect one row per student/course pair that has session history:
--   SELECT e.status, u.email, e.course_id, e.enrolled_at
--   FROM public.enrolments e JOIN public.users u ON u.id = e.student_id
--   ORDER BY u.email;
--
-- Anyone still missing (a student who signed up but never opened a session)
-- must redeem an invite code, or be added by their lecturer.
