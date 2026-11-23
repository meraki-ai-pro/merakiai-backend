-- 027: An instructor-set pass mark per exam (after 016).
--
-- NULL keeps the 50% every exam had before, so existing papers, results and
-- exports are unchanged until an instructor sets one. Used for the pass rate on
-- the Overview, the Data Center and the Excel export.
-- Idempotent.

BEGIN;

ALTER TABLE public.assessments ADD COLUMN IF NOT EXISTS pass_mark numeric
  CHECK (pass_mark IS NULL OR (pass_mark > 0 AND pass_mark <= 100));

COMMIT;
