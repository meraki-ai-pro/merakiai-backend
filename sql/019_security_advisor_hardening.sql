-- 019: Supabase security advisor hardening (2026-10-14).
--
-- Every function in public is EXECUTE-able by PUBLIC by default, and the
-- anon key ships in the browser bundle, so anything not revoked here is a
-- REST endpoint (/rest/v1/rpc/<name>) open to the internet. Two of them were
-- real holes:
--
--   accept_enrolment_invitations(p_user_id, p_email) — SECURITY DEFINER and
--     trusts both arguments. Anyone could pass their own id with a victim's
--     email and take the victim's pending course invitations.
--   anonymise_user_events(p_user_id) — anyone could strip attribution from
--     any user's research events.
--
-- Who keeps EXECUTE, decided from the callers in app/:
--   service_role only  accept_enrolment_invitations, anonymise_user_events
--                      (called with get_supabase()), visible_document_ids
--                      (not called at all — retrieval reads documents itself)
--   authenticated      is_admin, is_lecturer, is_super_admin, is_enrolled,
--                      owns_course (used inside 37 RLS policies, which run as
--                      the caller), redeem_invite_code (called with the
--                      student's own JWT; it reads auth.uid(), so it cannot
--                      act for anyone else)
--   nobody             trigger functions — Postgres checks EXECUTE when a
--                      trigger is created, not when it fires, so triggers keep
--                      working
-- The backend's anon client is used only for Auth calls (sign-in, refresh),
-- never for table queries, so no anon request reaches these policies.
--
-- Also pins search_path on the five functions that did not set it, so a
-- caller cannot shadow public tables with objects in their own schema.
--
-- Not changed here, on purpose:
--   vector extension in public — moving it re-points every vector column and
--     query; low risk, separate change if ever.
--   leaked-password protection — an Auth setting in the dashboard.
--   assessment_starts / embedding_cache with RLS on and no policies — that is
--     the intended "service role only" state.
--
-- Idempotent: REVOKE/GRANT/ALTER can run repeatedly.

BEGIN;

-- 1. Server-side only -------------------------------------------------------
REVOKE EXECUTE ON FUNCTION public.accept_enrolment_invitations(uuid, text) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.anonymise_user_events(uuid)            FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.visible_document_ids(text, text)       FROM PUBLIC, anon, authenticated;
GRANT  EXECUTE ON FUNCTION public.accept_enrolment_invitations(uuid, text) TO service_role;
GRANT  EXECUTE ON FUNCTION public.anonymise_user_events(uuid)            TO service_role;
GRANT  EXECUTE ON FUNCTION public.visible_document_ids(text, text)       TO service_role;

-- 2. Signed-in users only (RLS helpers + invite redemption) -----------------
REVOKE EXECUTE ON FUNCTION public.is_admin()                FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.is_lecturer()             FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.is_super_admin()          FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.is_enrolled(text)         FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.owns_course(text)         FROM PUBLIC, anon;
REVOKE EXECUTE ON FUNCTION public.redeem_invite_code(text)  FROM PUBLIC, anon;
GRANT  EXECUTE ON FUNCTION public.is_admin()                TO authenticated, service_role;
GRANT  EXECUTE ON FUNCTION public.is_lecturer()             TO authenticated, service_role;
GRANT  EXECUTE ON FUNCTION public.is_super_admin()          TO authenticated, service_role;
GRANT  EXECUTE ON FUNCTION public.is_enrolled(text)         TO authenticated, service_role;
GRANT  EXECUTE ON FUNCTION public.owns_course(text)         TO authenticated, service_role;
GRANT  EXECUTE ON FUNCTION public.redeem_invite_code(text)  TO authenticated, service_role;

-- 3. Trigger functions: never callable over REST ----------------------------
REVOKE EXECUTE ON FUNCTION public.handle_new_user()            FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.prevent_role_escalation()    FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.apply_avatar_voice_bundle()  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.enforce_avatar_voice_match() FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.normalise_invitation_email() FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.touch_updated_at()           FROM PUBLIC, anon, authenticated;

-- 4. Fixed search_path ------------------------------------------------------
ALTER FUNCTION public.apply_avatar_voice_bundle()  SET search_path = public, pg_temp;
ALTER FUNCTION public.enforce_avatar_voice_match() SET search_path = public, pg_temp;
ALTER FUNCTION public.handle_new_user()            SET search_path = public, pg_temp;
ALTER FUNCTION public.normalise_invitation_email() SET search_path = public, pg_temp;
ALTER FUNCTION public.touch_updated_at()           SET search_path = public, pg_temp;

COMMIT;

-- Verify (expect no anon/authenticated on the service-only functions):
--   SELECT proname, proacl FROM pg_proc
--   WHERE proname IN ('accept_enrolment_invitations','anonymise_user_events');
