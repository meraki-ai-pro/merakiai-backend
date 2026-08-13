-- Lecturer role + course ownership.
--
-- Introduces 'lecturer' as a first-class role distinct from 'admin', and gives
-- courses an owner so a lecturer can manage their own courses without being
-- granted platform-wide admin rights.
--
-- Ref: Meraki_AI_Integration_Roadmap §B.1 / §B.2
--      Meraki_AI_Lecturer_Side_Technical_Documentation §2.1, §6
--
-- Ordering matters here. Widening the role CHECK constraint (step 1) BEFORE
-- pinning down is_admin() (step 2) would open a window in which a lecturer
-- could satisfy an is_admin() written as `role <> 'user'`. Step 2 therefore
-- redefines the helper explicitly rather than trusting its current body.
--
-- Idempotent: safe to run more than once.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Role vocabulary
-- ---------------------------------------------------------------------------
-- Widening, not replacing: every existing role stays valid, so no backfill.

ALTER TABLE public.users
  DROP CONSTRAINT IF EXISTS users_role_check;

ALTER TABLE public.users
  ADD CONSTRAINT users_role_check
  CHECK (role = ANY (ARRAY[
    'user'::text,
    'lecturer'::text,
    'admin'::text,
    'super_admin'::text
  ]));


-- ---------------------------------------------------------------------------
-- 2. Role helper functions
-- ---------------------------------------------------------------------------
-- is_admin() is redefined explicitly. It is referenced by roughly thirty RLS
-- policies, so if its current body happened to be written as `role <> 'user'`
-- (rather than an explicit membership test) then merely adding 'lecturer' to
-- the enum above would silently hand every lecturer full admin read/write
-- across the entire database. Restating it removes that dependency on history.
--
-- A lecturer is deliberately NOT an admin. Lecturer authority is scoped to the
-- courses they own, and is expressed by owns_course() below.

CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.users
    WHERE id = auth.uid()
      AND role IN ('admin', 'super_admin')
  );
$$;

CREATE OR REPLACE FUNCTION public.is_super_admin()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.users
    WHERE id = auth.uid()
      AND role = 'super_admin'
  );
$$;

CREATE OR REPLACE FUNCTION public.is_lecturer()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.users
    WHERE id = auth.uid()
      AND role = 'lecturer'
  );
$$;


-- ---------------------------------------------------------------------------
-- 3. Course ownership columns
-- ---------------------------------------------------------------------------
-- courses.id is text (not uuid) — see TECHNICAL_DOCUMENTATION §14 — so any
-- table referencing it must use text for the FK. owner_id is nullable because
-- existing courses predate ownership; admins retain access to unowned courses.

ALTER TABLE public.courses
  ADD COLUMN IF NOT EXISTS owner_id uuid REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE public.courses
  ADD COLUMN IF NOT EXISTS academic_level text;

ALTER TABLE public.courses
  DROP CONSTRAINT IF EXISTS courses_academic_level_check;

ALTER TABLE public.courses
  ADD CONSTRAINT courses_academic_level_check
  CHECK (academic_level IS NULL OR academic_level = ANY (ARRAY[
    'foundation'::text,
    'intermediate'::text,
    'advanced'::text,
    'masters'::text,
    'doctoral'::text
  ]));

-- Application (Practice) mode is opt-out per course — Permission Checks §3.3.
-- Defaults true so existing courses keep their current behaviour exactly.
ALTER TABLE public.courses
  ADD COLUMN IF NOT EXISTS practice_mode_enabled boolean NOT NULL DEFAULT true;

ALTER TABLE public.courses
  ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();

CREATE INDEX IF NOT EXISTS courses_owner_id_idx ON public.courses (owner_id);


-- ---------------------------------------------------------------------------
-- 4. Ownership helper
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER so it can read courses regardless of the caller's own RLS
-- visibility; it answers one narrow question and leaks nothing else.

CREATE OR REPLACE FUNCTION public.owns_course(p_course_id text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.courses
    WHERE id = p_course_id
      AND owner_id = auth.uid()
  );
$$;


-- ---------------------------------------------------------------------------
-- 5. Role-escalation trigger
-- ---------------------------------------------------------------------------
-- Rules (Roadmap §B.1):
--   * nobody changes their own role
--   * granting OR revoking admin/super_admin is super_admin territory only
--     (previously an admin could mint another admin)
--   * user <-> lecturer transitions need admin or super_admin
--
-- auth.uid() IS NULL means the service-role key is in use, i.e. the FastAPI
-- backend. Those calls are already gated by admin_guard plus the explicit
-- checks in app/api/v1/admin/users.py, and the trigger must let them through
-- or /admin/users/{id}/role breaks. The trigger's real job is to stop a raw
-- PostgREST call made with a user JWT.

CREATE OR REPLACE FUNCTION public.prevent_role_escalation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  actor_id   uuid := auth.uid();
  actor_role text;
BEGIN
  IF NEW.role IS NOT DISTINCT FROM OLD.role THEN
    RETURN NEW;
  END IF;

  -- Service role (backend). Already authorised upstream.
  IF actor_id IS NULL THEN
    RETURN NEW;
  END IF;

  IF actor_id = NEW.id THEN
    RAISE EXCEPTION 'You cannot change your own role';
  END IF;

  SELECT role INTO actor_role FROM public.users WHERE id = actor_id;

  IF NEW.role IN ('admin', 'super_admin')
     OR OLD.role IN ('admin', 'super_admin') THEN
    IF actor_role IS DISTINCT FROM 'super_admin' THEN
      RAISE EXCEPTION 'Only a super admin may grant or revoke admin roles';
    END IF;
    RETURN NEW;
  END IF;

  IF actor_role NOT IN ('admin', 'super_admin') THEN
    RAISE EXCEPTION 'Only an admin may change user roles';
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS prevent_role_escalation_trigger ON public.users;

CREATE TRIGGER prevent_role_escalation_trigger
  BEFORE UPDATE OF role ON public.users
  FOR EACH ROW
  EXECUTE FUNCTION public.prevent_role_escalation();


-- ---------------------------------------------------------------------------
-- 6. Course RLS
-- ---------------------------------------------------------------------------
-- Existing policies (courses_select_authenticated, courses_admin_write) are
-- left in place. This adds lecturer authority scoped to owned courses only.

DROP POLICY IF EXISTS courses_lecturer_write ON public.courses;

CREATE POLICY courses_lecturer_write ON public.courses
  FOR ALL
  USING (public.owns_course(id))
  WITH CHECK (public.owns_course(id));

-- A lecturer creating a course must set themselves as owner — without this
-- the USING clause above can never be satisfied for a brand-new row.
DROP POLICY IF EXISTS courses_lecturer_insert ON public.courses;

CREATE POLICY courses_lecturer_insert ON public.courses
  FOR INSERT
  WITH CHECK (owner_id = auth.uid() AND public.is_lecturer());

COMMIT;


-- Verify:
--   SELECT pg_get_constraintdef(oid) FROM pg_constraint
--   WHERE conrelid = 'public.users'::regclass AND conname = 'users_role_check';
--
--   SELECT prosrc FROM pg_proc WHERE proname = 'is_admin';
--
--   SELECT column_name, data_type, column_default FROM information_schema.columns
--   WHERE table_name = 'courses' AND column_name IN
--     ('owner_id','academic_level','practice_mode_enabled');
--
--   SELECT polname, pg_get_expr(polqual, polrelid) FROM pg_policy
--   WHERE polrelid = 'public.courses'::regclass;
