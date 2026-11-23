-- 017: Targeted assessments, for the Intervention Studio.
--
-- The studio drafts short practice papers, 10-minute diagnostics and delayed
-- retrieval checks for the students a lecturer picked — the eleven who share a
-- misconception, not the whole class. Until now every published paper went to
-- everyone enrolled.
--
-- assessments.target_student_ids: NULL means the whole course (every existing
-- paper, unchanged). A non-empty array means only those students see it, can
-- take it, or can read its results. The API enforces this on every student
-- path; students never read the table directly.
--
-- Requires 010 (assessments). Idempotent.

BEGIN;

ALTER TABLE public.assessments
  ADD COLUMN IF NOT EXISTS target_student_ids uuid[];

COMMENT ON COLUMN public.assessments.target_student_ids IS
  'NULL = whole course. Otherwise only these students may see or take the paper.';

COMMIT;

-- Verify:
--   SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'assessments' AND column_name = 'target_student_ids';
