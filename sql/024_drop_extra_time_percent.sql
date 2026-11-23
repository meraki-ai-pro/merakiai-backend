-- 024: Drop the percentage extra-time column (after 022).
--
-- APPLY ONLY AFTER the backend that reads extra_time_minutes is deployed. The
-- previous backend selects extra_time_percent; dropping it first breaks the
-- exam list and the accommodations panel.
--
-- 022 replaced it with minutes; no row ever carried a non-zero percentage.
-- Idempotent.

BEGIN;

ALTER TABLE public.enrolments DROP COLUMN IF EXISTS extra_time_percent;

COMMIT;
