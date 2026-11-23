-- 026: A course's instructor is never one of its students (after 003, 004, 020).
--
-- Instructors already open their own course without enrolling (require_enrolment
-- admits the owner), so an enrolment of theirs is always a mistake — usually a
-- test of the invite code. It then counts them as a student in every class
-- list, analytics figure, exam and Intervention Studio "whole class".
--
-- Enforced here rather than in each caller: invite redemption, add-by-email,
-- roster import and invitation acceptance all insert into enrolments, and a
-- trigger covers the ones written later too.
--
-- Idempotent.

BEGIN;

CREATE OR REPLACE FUNCTION private.reject_owner_enrolment()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.courses c
     WHERE c.id = NEW.course_id AND c.owner_id = NEW.student_id
  ) THEN
    RAISE EXCEPTION 'The course instructor cannot enrol as a student on their own course'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

REVOKE EXECUTE ON FUNCTION private.reject_owner_enrolment() FROM PUBLIC;

DROP TRIGGER IF EXISTS enrolments_reject_owner ON public.enrolments;
CREATE TRIGGER enrolments_reject_owner
  BEFORE INSERT OR UPDATE OF student_id, course_id ON public.enrolments
  FOR EACH ROW EXECUTE FUNCTION private.reject_owner_enrolment();

-- Any enrolment that already exists for a course's own instructor is the
-- mistake above; removing it takes them out of the class list and counts.
DELETE FROM public.enrolments e
 USING public.courses c
 WHERE c.id = e.course_id AND c.owner_id = e.student_id;

COMMIT;
