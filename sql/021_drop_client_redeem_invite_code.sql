-- 021: Remove the client-callable invite redemption (after 020).
--
-- APPLY ONLY AFTER the backend that calls redeem_invite_code_for() is
-- deployed. An older backend calls redeem_invite_code() with the student's
-- JWT, and dropping it first makes every "Join with an invite code" fail.
--
-- Clears the last security-advisor warning about SECURITY DEFINER functions
-- callable by signed-in users. Idempotent.

BEGIN;

DROP FUNCTION IF EXISTS public.redeem_invite_code(text);

COMMIT;
