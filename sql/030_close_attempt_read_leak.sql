-- 030: Students could read their own exam attempts straight through PostgREST
-- (anon key + their JWT are in the browser), including per-question
-- is_correct, score and the AI's draft feedback BEFORE the lecturer releases
-- results. The API reads attempts with the service role, and the frontend never
-- queries this table directly, so the student policy has no legitimate use.
-- Results reach students only through GET /assessments/{id}/my-result, which
-- honours results_released.
-- Idempotent and transactional. Safe to apply before or after the code change.
BEGIN;
DROP POLICY IF EXISTS attempts_student_select ON public.assessment_attempts;
COMMIT;
