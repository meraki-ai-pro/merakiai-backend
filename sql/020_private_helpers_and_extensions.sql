-- 020: Clear the remaining Supabase security-advisor warnings (2026-10-14).
--
-- After 019 three kinds were left:
--
-- 1. RLS helpers callable over REST by any signed-in user
--    (is_admin, is_lecturer, is_super_admin, is_enrolled, owns_course).
--    They only ever answer about the caller, but nothing needs to call them as
--    /rest/v1/rpc/..., so they move to a `private` schema PostgREST does not
--    expose. RLS policies reference functions by OID, not by name, so all 37
--    policies keep working untouched; no function body calls these by name.
--    Signed-in users keep EXECUTE (and USAGE on the schema) because the
--    policies run as the caller.
--
--    CONSEQUENCE FOR LATER SQL: write policies as `private.is_admin()`, not
--    `public.is_admin()`. Re-running 003/004 on this database would recreate
--    unused public copies — harmless, but don't.
--
-- 2. pgvector installed in `public`. Moved to Supabase's `extensions` schema.
--    The only vector column (embedding_cache.embedding) follows by OID, and
--    the backend writes it as a JSON array that Postgres casts by column type.
--
-- 3. redeem_invite_code callable by signed-in users. It reads auth.uid(), so
--    it could only ever act for the caller, but it is a SECURITY DEFINER
--    function on the public API. redeem_invite_code_for(student, code) is the
--    same transaction with the student passed in, and only the service role
--    may call it; the backend passes the id from the verified JWT. The old
--    function stays until the backend that calls the new one is deployed —
--    021 then removes it.
--
-- Idempotent.

BEGIN;

-- 1. Helpers out of the exposed API --------------------------------------
CREATE SCHEMA IF NOT EXISTS private;
REVOKE ALL ON SCHEMA private FROM PUBLIC;
GRANT USAGE ON SCHEMA private TO authenticated, service_role;

DO $$
DECLARE
  f text;
BEGIN
  FOREACH f IN ARRAY ARRAY['is_admin()', 'is_lecturer()', 'is_super_admin()',
                           'is_enrolled(text)', 'owns_course(text)']
  LOOP
    IF to_regprocedure('public.' || f) IS NOT NULL THEN
      EXECUTE format('ALTER FUNCTION public.%s SET SCHEMA private', f);
    END IF;
  END LOOP;
END $$;

-- 2. pgvector to the extensions schema -----------------------------------
CREATE SCHEMA IF NOT EXISTS extensions;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
             WHERE e.extname = 'vector' AND n.nspname = 'public') THEN
    ALTER EXTENSION vector SET SCHEMA extensions;
  END IF;
END $$;

-- 3. Invite redemption, server-side --------------------------------------
CREATE OR REPLACE FUNCTION public.redeem_invite_code_for(p_student_id uuid, p_code text)
RETURNS public.enrolments
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_code  public.invite_codes;
  v_enrol public.enrolments;
BEGIN
  IF p_student_id IS NULL THEN
    RAISE EXCEPTION 'Not authenticated' USING ERRCODE = '28000';
  END IF;

  SELECT * INTO v_code
    FROM public.invite_codes
   WHERE upper(code) = upper(btrim(p_code))
     FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Invalid invite code' USING ERRCODE = 'P0002';
  END IF;
  IF NOT v_code.is_active THEN
    RAISE EXCEPTION 'This invite code is no longer active' USING ERRCODE = 'P0002';
  END IF;
  IF v_code.expires_at IS NOT NULL AND v_code.expires_at <= now() THEN
    RAISE EXCEPTION 'This invite code has expired' USING ERRCODE = 'P0002';
  END IF;
  IF v_code.max_uses IS NOT NULL AND v_code.uses_count >= v_code.max_uses THEN
    RAISE EXCEPTION 'This invite code has been fully used' USING ERRCODE = 'P0002';
  END IF;

  SELECT * INTO v_enrol
    FROM public.enrolments
   WHERE course_id = v_code.course_id
     AND student_id = p_student_id;

  IF FOUND THEN
    -- Re-entering after withdrawal reactivates and costs a use. Redeeming a
    -- code already used is a no-op: students double-submit constantly.
    IF v_enrol.status = 'withdrawn' THEN
      UPDATE public.enrolments
         SET status = 'active', withdrawn_at = NULL, enrolled_at = now(), updated_at = now()
       WHERE id = v_enrol.id
       RETURNING * INTO v_enrol;

      UPDATE public.invite_codes SET uses_count = uses_count + 1 WHERE id = v_code.id;
    END IF;
    RETURN v_enrol;
  END IF;

  INSERT INTO public.enrolments (course_id, student_id, status)
  VALUES (v_code.course_id, p_student_id, 'active')
  RETURNING * INTO v_enrol;

  UPDATE public.invite_codes SET uses_count = uses_count + 1 WHERE id = v_code.id;
  RETURN v_enrol;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.redeem_invite_code_for(uuid, text) FROM PUBLIC, anon, authenticated;
GRANT  EXECUTE ON FUNCTION public.redeem_invite_code_for(uuid, text) TO service_role;

COMMIT;
