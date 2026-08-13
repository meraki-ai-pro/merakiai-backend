"""Lecturer API behaviour.

The recurring property: passing lecturer_guard is not authorisation. Every
course-scoped handler must call assert_course_owner(), or one lecturer reaches
another's students and knowledge base.
"""

import ast
import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.v1.lecturer import analytics, courses, knowledge, students
from app.api.v1.lecturer.courses import _COURSE_ID_RE, CourseCreate, create_course

BACKEND = Path(__file__).resolve().parents[2]

MODULES = (courses, knowledge, students, analytics)


def _route_handlers(module):
    """Every function in the module decorated with an HTTP method."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(func, ast.Attribute) and func.attr in {
                "get", "post", "patch", "delete", "put"
            }:
                path = ""
                if isinstance(dec, ast.Call) and dec.args:
                    arg = dec.args[0]
                    if isinstance(arg, ast.Constant):
                        path = arg.value
                out.append((node, path, module))
    return out


ALL_HANDLERS = [h for m in MODULES for h in _route_handlers(m)]


class TestOwnershipIsAlwaysChecked:
    def test_every_course_scoped_handler_asserts_ownership(self):
        """The check that stops cross-lecturer access. A handler taking
        course_id and not calling assert_course_owner is a data leak."""
        missing = []
        for node, path, module in ALL_HANDLERS:
            takes_course = any(
                a.arg == "course_id" for a in node.args.args + node.args.kwonlyargs
            )
            if not takes_course:
                continue
            body = ast.dump(node)
            if "assert_course_owner" not in body:
                missing.append(f"{module.__name__}.{node.name}")
        assert not missing, f"handlers missing the ownership check: {missing}"

    def test_there_are_course_scoped_handlers_to_check(self):
        """Guards the test above against silently passing on an empty set."""
        assert len(ALL_HANDLERS) >= 10

    def test_every_handler_requires_the_lecturer_guard(self):
        missing = []
        for node, _path, module in ALL_HANDLERS:
            src = ast.dump(node)
            if "lecturer_guard" not in src:
                missing.append(f"{module.__name__}.{node.name}")
        assert not missing, f"handlers missing lecturer_guard: {missing}"


class TestCourseIdValidation:
    @pytest.mark.parametrize(
        "cid", ["calculus-101", "stats-2", "froth-flotation", "a-b-c"]
    )
    def test_accepts_slugs(self, cid):
        assert _COURSE_ID_RE.match(cid)

    @pytest.mark.parametrize(
        "cid",
        [
            "Calculus 101",     # spaces and capitals
            "../etc/passwd",    # traversal — this lands in storage paths
            "maths_101",        # underscore
            "-leading",
            "trailing-",
            "a",                # too short
            "café-101",         # non-ascii
        ],
    )
    def test_rejects_anything_unsafe(self, cid):
        """course_id becomes a Pinecone namespace and a storage path segment,
        so it cannot be free text."""
        assert not _COURSE_ID_RE.match(cid)


class TestOwnerCannotBeSpoofed:
    def test_create_forces_the_caller_as_owner(self, monkeypatch):
        """Taking owner_id from the payload would let a lecturer create a
        course owned by someone else."""
        captured = {}

        class FakeTable:
            def select(self, *_a):
                return self

            def eq(self, *_a):
                return self

            def insert(self, row):
                captured.update(row)
                return self

            def execute(self):
                return type("R", (), {"data": [captured] if captured else []})()

        monkeypatch.setattr(
            courses, "get_supabase", lambda: type("S", (), {"table": lambda s, n: FakeTable()})()
        )
        monkeypatch.setattr(courses.audit, "record", lambda **k: None)

        user = {"id": "lec-1", "role": "lecturer"}
        create_course(CourseCreate(id="maths-101", name="Maths"), request=None, user=user)
        assert captured["owner_id"] == "lec-1"

    def test_payload_has_no_owner_field_at_all(self):
        """Belt and braces: the model cannot even carry an owner_id."""
        assert "owner_id" not in CourseCreate.model_fields


class TestKnowledgeDefaults:
    def test_lecturer_upload_defaults_to_draft(self):
        """Unlike the admin route. The lecturer flow is upload → test-query →
        publish; defaulting to published skips the review the draft exists for."""
        sig = inspect.signature(knowledge.upload_knowledge)
        assert sig.parameters["is_published"].default is False

    def test_delete_is_a_soft_delete(self):
        src = Path(knowledge.__file__).read_text(encoding="utf-8")
        assert "deleted_at" in src
        assert ".delete()" not in src

    def test_publish_changes_invalidate_the_visibility_cache(self):
        """The retriever caches visibility for 15s; without this an unpublished
        file keeps answering questions."""
        src = Path(knowledge.__file__).read_text(encoding="utf-8")
        assert src.count("invalidate_visibility(course_id)") >= 2

    def test_upload_validates_magic_bytes(self):
        src = Path(knowledge.__file__).read_text(encoding="utf-8")
        assert "_MAGIC[ext]" in src


class TestAuditTrail:
    @pytest.mark.parametrize(
        "module,action",
        [
            (courses, "course.create"),
            (courses, "course.update"),
            (knowledge, "knowledge.upload"),
            (knowledge, "knowledge.update"),
            (knowledge, "knowledge.delete"),
            (students, "invite.create"),
            (students, "enrolment.add"),
        ],
    )
    def test_mutations_are_logged(self, module, action):
        src = Path(module.__file__).read_text(encoding="utf-8")
        assert action in src

    def test_audit_write_never_raises(self):
        """It runs after an action that already succeeded; raising would turn a
        completed enrolment into a 500."""
        from app.core import audit as audit_mod

        def boom():
            raise RuntimeError("audit table gone")

        original = audit_mod.get_supabase
        audit_mod.get_supabase = boom
        try:
            audit_mod.record(
                actor={"id": "u", "role": "lecturer"},
                action="x", resource_type="y",
            )
        finally:
            audit_mod.get_supabase = original


class TestAnalyticsHonesty:
    def test_unmeasurable_metrics_are_declared_not_zeroed(self):
        """A zero would read as 'no learning happened', so anything without an
        instrument is named rather than shown as a number.

        The list shrank when #20/#21 landed — mastery and pre/post gains are
        measured now. time_on_task still has no instrument.
        """
        src = Path(analytics.__file__).read_text(encoding="utf-8")
        assert '"unavailable"' in src
        assert "time_on_task" in src

    def test_measured_sections_say_so_explicitly(self):
        """Both report measured: False with a reason rather than zeros, so an
        empty cohort never looks like a failed one."""
        src = Path(analytics.__file__).read_text(encoding="utf-8")
        assert src.count('"measured": False') >= 2
        assert src.count('"reason"') >= 2

    def test_one_failing_metric_does_not_fail_the_whole_dashboard(self):
        assert analytics._safe(lambda: 1 / 0, default="fallback") == "fallback"


class TestStudentManagement:
    def test_adding_an_unknown_email_explains_invite_codes(self):
        """A lecturer cannot create accounts; the error has to say what to do
        instead or they will assume it silently worked."""
        src = Path(students.__file__).read_text(encoding="utf-8")
        assert "invite code" in src

    def test_invite_codes_are_deactivated_not_deleted(self):
        """Students who already redeemed one keep their enrolment."""
        src = Path(students.__file__).read_text(encoding="utf-8")
        assert '"is_active": False' in src

    def test_invite_creation_retries_on_collision(self):
        src = Path(students.__file__).read_text(encoding="utf-8")
        assert "for _ in range(5)" in src

    def test_completion_does_not_clear_access(self):
        """Permission Checks §3.2 — a completed student keeps read access."""
        src = Path(students.__file__).read_text(encoding="utf-8")
        assert "completed_at" in src
        assert "withdrawn_at" in src


class TestCohortCountsExcludeStaff:
    """Caught in live end-to-end, not by unit tests: a course whose sessions
    include an admin or the lecturer themselves reported more students started
    than were enrolled ("7 of 5"), and zeroed enrolled_but_never_started."""

    def test_started_count_is_intersected_with_enrolments(self):
        src = Path(analytics.__file__).read_text(encoding="utf-8")
        assert "& enrolled_ids" in src

    def test_never_started_is_not_clamped_by_max(self):
        """max(0, ...) hid the bug instead of surfacing it."""
        src = Path(analytics.__file__).read_text(encoding="utf-8")
        assert "max(0, len(set(student_ids))" not in src

    def test_staff_sessions_are_reported_separately(self):
        src = Path(analytics.__file__).read_text(encoding="utf-8")
        assert "non_student_session_users" in src
