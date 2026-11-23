-- 022: Exam extra time in minutes rather than a percentage (after 016).
--
-- Testers found a percentage hard to reason about ("25% of what?"); an
-- accommodation letter states minutes. The same minutes are added to every
-- timed paper on the course.
--
-- extra_time_percent is left in place (unused from this backend on) so an
-- older deployed backend keeps working until this one ships. No row carried a
-- non-zero percentage when this was written, so nothing needed converting.
-- Idempotent.

BEGIN;

ALTER TABLE public.enrolments ADD COLUMN IF NOT EXISTS extra_time_minutes integer
  NOT NULL DEFAULT 0 CHECK (extra_time_minutes BETWEEN 0 AND 600);

COMMIT;
