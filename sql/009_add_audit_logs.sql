-- Audit log.
--
-- The lecturer spec requires every upload, delete and enrolment change to be
-- recorded (§6 Security & Isolation, §3.1). Without it there is no way to
-- answer "who removed this student" or "who unpublished the exam paper" — and
-- in a research pilot with informed consent, that is not a rhetorical question.
--
-- Ref: AI_Teaching_System_Technical_Specification_v3 §6.6
--      Meraki_AI_Lecturer_Side_Technical_Documentation §6
--
-- Idempotent: safe to run more than once.

BEGIN;

CREATE TABLE IF NOT EXISTS public.audit_logs (
  id            uuid        NOT NULL DEFAULT gen_random_uuid(),
  actor_id      uuid        REFERENCES public.users(id) ON DELETE SET NULL,
  actor_role    text,
  action        text        NOT NULL,
  resource_type text        NOT NULL,
  resource_id   text,
  course_id     text        REFERENCES public.courses(id) ON DELETE SET NULL,
  old_values    jsonb,
  new_values    jsonb,
  ip_address    text,
  user_agent    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT audit_logs_pkey PRIMARY KEY (id)
);

-- actor_id is nullable and ON DELETE SET NULL on purpose: deleting a user must
-- not delete the record of what they did. The trail outlives the account.

CREATE INDEX IF NOT EXISTS audit_logs_course_idx ON public.audit_logs (course_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_logs_actor_idx  ON public.audit_logs (actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_logs_resource_idx ON public.audit_logs (resource_type, resource_id);

ALTER TABLE public.audit_logs ENABLE ROW LEVEL SECURITY;

-- Read-only to lecturers, for their own courses. Nobody gets UPDATE or DELETE
-- through the API — an audit log an actor can edit is not an audit log. Writes
-- go through the service role only.
DROP POLICY IF EXISTS audit_logs_lecturer_select ON public.audit_logs;
CREATE POLICY audit_logs_lecturer_select ON public.audit_logs
  FOR SELECT USING (public.owns_course(course_id));

DROP POLICY IF EXISTS audit_logs_admin_select ON public.audit_logs;
CREATE POLICY audit_logs_admin_select ON public.audit_logs
  FOR SELECT USING (public.is_admin());

COMMIT;


-- Verify:
--   SELECT polname, cmd FROM pg_policies WHERE tablename = 'audit_logs';
--   SELECT action, resource_type, created_at FROM public.audit_logs
--   ORDER BY created_at DESC LIMIT 20;
