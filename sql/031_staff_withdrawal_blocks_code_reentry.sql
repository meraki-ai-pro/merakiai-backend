-- 031: A student withdrawn by a lecturer could re-enrol with the class invite
-- code, undoing the withdrawal. Record who withdrew them and make code
-- redemption refuse to reactivate a staff-withdrawn enrolment. A student who
-- withdrew themselves (or whose account was deactivated) can still return.
-- Idempotent and transactional. Apply before relying on the lecturer-side code
-- change (the API degrades to the old behaviour if this is missing).
BEGIN;

ALTER TABLE public.enrolments
  ADD COLUMN IF NOT EXISTS removed_by_staff boolean NOT NULL DEFAULT false;

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
    IF v_enrol.status = 'withdrawn' THEN
      IF v_enrol.removed_by_staff THEN
        RAISE EXCEPTION 'You were removed from this course. Ask your instructor to re-enrol you.'
          USING ERRCODE = 'P0002';
      END IF;

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
