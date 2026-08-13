-- Enrolments + invite codes.
--
-- Gives the platform a first-class answer to "is this student allowed on this
-- course?", which today it cannot express at all (Student Permission Checks
-- §2). Also carries the completion-vs-departure distinction the lifecycle work
-- depends on.
--
-- Ref: Meraki_AI_Integration_Roadmap §B.2, Part C, Part D
--      Meraki_AI_Student_Permission_Checks §3.2
--
-- PREREQUISITE: sql/003_add_lecturer_role_and_course_ownership.sql must be applied
-- first — the policies below call owns_course(), and invite codes are issued
-- by course owners.
--
-- Idempotent: safe to run more than once.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Enrolments
-- ---------------------------------------------------------------------------
-- course_id is text because courses.id is text (TECHNICAL_DOCUMENTATION §14),
-- not uuid like every other key in the schema.

CREATE TABLE IF NOT EXISTS public.enrolments (
  id           uuid        NOT NULL DEFAULT gen_random_uuid(),
  course_id    text        NOT NULL REFERENCES public.courses(id) ON DELETE CASCADE,
  student_id   uuid        NOT NULL REFERENCES public.users(id)   ON DELETE CASCADE,
  status       text        NOT NULL DEFAULT 'active'
                           CHECK (status = ANY (ARRAY[
                             'active'::text,
                             'completed'::text,
                             'withdrawn'::text,
                             'archived'::text
                           ])),
  enrolled_at  timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  withdrawn_at timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT enrolments_pkey PRIMARY KEY (id),
  CONSTRAINT enrolments_course_student_key UNIQUE (course_id, student_id)
);

CREATE INDEX IF NOT EXISTS enrolments_student_idx ON public.enrolments (student_id);
CREATE INDEX IF NOT EXISTS enrolments_course_idx  ON public.enrolments (course_id);
-- Supports the hot path: "is this student active on this course right now?"
CREATE INDEX IF NOT EXISTS enrolments_lookup_idx
  ON public.enrolments (student_id, course_id, status);


-- ---------------------------------------------------------------------------
-- 2. Invite codes
-- ---------------------------------------------------------------------------
-- max_uses NULL means unlimited; expires_at NULL means never expires.

CREATE TABLE IF NOT EXISTS public.invite_codes (
  id          uuid        NOT NULL DEFAULT gen_random_uuid(),
  course_id   text        NOT NULL REFERENCES public.courses(id) ON DELETE CASCADE,
  code        text        NOT NULL,
  created_by  uuid        REFERENCES public.users(id) ON DELETE SET NULL,
  max_uses    integer     CHECK (max_uses IS NULL OR max_uses > 0),
  uses_count  integer     NOT NULL DEFAULT 0,
  expires_at  timestamptz,
  is_active   boolean     NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT invite_codes_pkey PRIMARY KEY (id)
);

-- Case-insensitive uniqueness: students will type these by hand from a
-- whiteboard or a WhatsApp message, so 'MATH101' and 'math101' must not be
-- two different codes.
CREATE UNIQUE INDEX IF NOT EXISTS invite_codes_code_key
  ON public.invite_codes (upper(code));

CREATE INDEX IF NOT EXISTS invite_codes_course_idx ON public.invite_codes (course_id);


-- ---------------------------------------------------------------------------
-- 3. Atomic redemption
-- ---------------------------------------------------------------------------
-- Roadmap §E requires redemption to be one transaction. Doing this in
-- application code cannot be made safe: two students redeeming the last
-- remaining use concurrently would both read uses_count = max_uses - 1 and
-- both succeed. FOR UPDATE serialises them on the code row.
--
-- SECURITY DEFINER because students must be able to redeem a code without
-- being granted SELECT on invite_codes — otherwise anyone could enumerate
-- every course's codes.

CREATE OR REPLACE FUNCTION public.redeem_invite_code(p_code text)
RETURNS public.enrolments
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_student uuid := auth.uid();
  v_code    public.invite_codes;
  v_enrol   public.enrolments;
BEGIN
  IF v_student IS NULL THEN
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
     AND student_id = v_student;

  IF FOUND THEN
    -- Re-entering after withdrawal reactivates and costs a use. Redeeming a
    -- code you have already used is a no-op and costs nothing — students
    -- double-submit constantly and must not burn a seat doing so.
    IF v_enrol.status = 'withdrawn' THEN
      UPDATE public.enrolments
         SET status = 'active', withdrawn_at = NULL, enrolled_at = now(), updated_at = now()
       WHERE id = v_enrol.id
       RETURNING * INTO v_enrol;

      UPDATE public.invite_codes
         SET uses_count = uses_count + 1
       WHERE id = v_code.id;
    END IF;

    RETURN v_enrol;
  END IF;

  INSERT INTO public.enrolments (course_id, student_id, status)
  VALUES (v_code.course_id, v_student, 'active')
  RETURNING * INTO v_enrol;

  UPDATE public.invite_codes
     SET uses_count = uses_count + 1
   WHERE id = v_code.id;

  RETURN v_enrol;
END;
$$;

REVOKE ALL ON FUNCTION public.redeem_invite_code(text) FROM public;
GRANT EXECUTE ON FUNCTION public.redeem_invite_code(text) TO authenticated;


-- ---------------------------------------------------------------------------
-- 4. Enrolment lookup helper
-- ---------------------------------------------------------------------------
-- Used by RLS on other tables (and later by the retriever) to answer
-- "may this student see this course's material?" without a join everywhere.

CREATE OR REPLACE FUNCTION public.is_enrolled(p_course_id text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.enrolments
    WHERE course_id = p_course_id
      AND student_id = auth.uid()
      AND status IN ('active', 'completed')
  );
$$;


-- ---------------------------------------------------------------------------
-- 5. RLS
-- ---------------------------------------------------------------------------

ALTER TABLE public.enrolments   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invite_codes ENABLE ROW LEVEL SECURITY;

-- Students read their own enrolments and nothing else. They never write:
-- creation goes through redeem_invite_code(), status changes through the
-- lecturer, so a student cannot self-promote from withdrawn back to active.
DROP POLICY IF EXISTS enrolments_student_select ON public.enrolments;
CREATE POLICY enrolments_student_select ON public.enrolments
  FOR SELECT USING (student_id = auth.uid());

DROP POLICY IF EXISTS enrolments_lecturer_all ON public.enrolments;
CREATE POLICY enrolments_lecturer_all ON public.enrolments
  FOR ALL
  USING (public.owns_course(course_id))
  WITH CHECK (public.owns_course(course_id));

DROP POLICY IF EXISTS enrolments_admin_all ON public.enrolments;
CREATE POLICY enrolments_admin_all ON public.enrolments
  FOR ALL USING (public.is_admin()) WITH CHECK (public.is_admin());

-- No student-facing policy on invite_codes at all. Redemption is the only
-- student interaction and it runs SECURITY DEFINER, so granting SELECT here
-- would only enable enumeration.
DROP POLICY IF EXISTS invite_codes_lecturer_all ON public.invite_codes;
CREATE POLICY invite_codes_lecturer_all ON public.invite_codes
  FOR ALL
  USING (public.owns_course(course_id))
  WITH CHECK (public.owns_course(course_id));

DROP POLICY IF EXISTS invite_codes_admin_all ON public.invite_codes;
CREATE POLICY invite_codes_admin_all ON public.invite_codes
  FOR ALL USING (public.is_admin()) WITH CHECK (public.is_admin());


-- ---------------------------------------------------------------------------
-- 6. updated_at maintenance
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS enrolments_touch_updated_at ON public.enrolments;
CREATE TRIGGER enrolments_touch_updated_at
  BEFORE UPDATE ON public.enrolments
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

COMMIT;


-- Verify:
--   SELECT tablename, rowsecurity FROM pg_tables
--   WHERE tablename IN ('enrolments','invite_codes');
--
--   SELECT polname, cmd FROM pg_policies
--   WHERE tablename IN ('enrolments','invite_codes');
--
--   -- concurrency check: two sessions, same last-remaining-use code,
--   -- exactly one should succeed
--   SELECT public.redeem_invite_code('MATH101');
