-- Instructor decisions are separate from earned question marks.
BEGIN;
CREATE TABLE IF NOT EXISTS public.assessment_integrity_reviews (
  assessment_id uuid NOT NULL REFERENCES public.assessments(id) ON DELETE CASCADE,
  student_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  integrity_status text NOT NULL CHECK (integrity_status IN ('clear', 'concern', 'confirmed')),
  deduction numeric NOT NULL DEFAULT 0 CHECK (deduction >= 0),
  feedback text NOT NULL,
  reviewed_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
  reviewed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (assessment_id, student_id),
  CHECK (deduction = 0 OR integrity_status = 'confirmed')
);
ALTER TABLE public.assessment_integrity_reviews ENABLE ROW LEVEL SECURITY;
-- API service role only. Student decisions are exposed only after release.
COMMIT;
