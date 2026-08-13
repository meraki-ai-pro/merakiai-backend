-- Replace the generic academic ladder with the Ghanaian one.
--
-- The original constraint used foundation/intermediate/advanced/masters/
-- doctoral. Nobody at a Ghanaian university speaks that way: students say
-- "Level 200", lecturers write MATH 103, and the technical universities award
-- HNDs. A vocabulary the pilot cohort has to translate at every screen is
-- friction we chose to introduce for no benefit.
--
-- The internal SCAFFOLDING TIER keeps the old grouping — see
-- app/core/academic_levels.py — because several levels are taught the same
-- way. Only the user-facing vocabulary changes.
--
-- Safe to run now: courses.academic_level is NULL on every existing row, so
-- there is nothing to migrate. The UPDATE below is defensive in case a value
-- was set between writing this and running it.
--
-- Idempotent: safe to run more than once.

BEGIN;

-- Old values, mapped rather than dropped. Level 100 is the pilot cohort, so
-- 'foundation' becomes level_100 and anything already 'intermediate' or
-- 'advanced' lands on the year that tier corresponded to.
UPDATE public.courses
   SET academic_level = CASE academic_level
         WHEN 'foundation'   THEN 'level_100'
         WHEN 'intermediate' THEN 'level_300'
         WHEN 'advanced'     THEN 'level_400'
         ELSE academic_level          -- masters and doctoral are unchanged
       END
 WHERE academic_level IN ('foundation', 'intermediate', 'advanced');

ALTER TABLE public.courses
  DROP CONSTRAINT IF EXISTS courses_academic_level_check;

ALTER TABLE public.courses
  ADD CONSTRAINT courses_academic_level_check
  CHECK (academic_level IS NULL OR academic_level = ANY (ARRAY[
    'access'::text,      -- pre-degree / mature entry
    'level_100'::text,   -- first year
    'level_200'::text,   -- second year
    'level_300'::text,   -- third year
    'level_400'::text,   -- final year
    'level_500'::text,   -- fifth year: Medicine, Pharmacy, Architecture, Vet
    'level_600'::text,   -- sixth year: Medicine
    'hnd'::text,         -- HND / Diploma, technical universities
    'masters'::text,     -- MPhil / MSc / MA / MBA
    'doctoral'::text     -- PhD
  ]));

COMMIT;


-- Verify:
--   SELECT pg_get_constraintdef(oid) FROM pg_constraint
--   WHERE conname = 'courses_academic_level_check';
--
--   SELECT id, name, academic_level FROM public.courses;
