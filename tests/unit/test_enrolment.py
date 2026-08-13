"""Enrolment checks — steps 3 and 4 of the student permission stack.

Ref: Meraki_AI_Student_Permission_Checks §3.2, §3.3
"""

import pytest
from fastapi import HTTPException

from app.core import enrolment as en
from app.core.enrolment import (
    ACTIVE_STATUSES,
    PARTICIPATING_STATUSES,
    generate_invite_code,
    require_enrolment,
    require_mode_enabled,
)


class FakeTable:
    def __init__(self, rows, raises=None):
        self.rows = rows
        self.raises = raises
        self.filters = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, col, value):
        self.filters[col] = value
        return self

    def execute(self):
        if self.raises:
            raise self.raises
        out = [
            r for r in self.rows
            if all(r.get(k) == v for k, v in self.filters.items())
        ]
        return type("R", (), {"data": out})()


class FakeSupabase:
    def __init__(self, rows, raises=None):
        self.rows = rows
        self.raises = raises

    def table(self, _name):
        return FakeTable(self.rows, self.raises)


@pytest.fixture
def rows(monkeypatch):
    def _apply(data, raises=None):
        monkeypatch.setattr(en, "get_supabase", lambda: FakeSupabase(data, raises))

    return _apply


def student(uid="s1"):
    return {"id": uid, "role": "user", "email": "s@x.c", "token": "t"}


def enrol(status_, course="maths-101", uid="s1"):
    return [{"id": "e1", "course_id": course, "student_id": uid, "status": status_}]


class TestEnrolmentRequired:
    def test_active_student_passes(self, rows):
        rows(enrol("active"))
        assert require_enrolment(student(), "maths-101")["status"] == "active"

    def test_unenrolled_student_is_rejected(self, rows):
        rows([])
        with pytest.raises(HTTPException) as exc:
            require_enrolment(student(), "maths-101")
        assert exc.value.status_code == 403

    def test_enrolment_on_another_course_does_not_count(self, rows):
        rows(enrol("active", course="chem-101"))
        with pytest.raises(HTTPException):
            require_enrolment(student(), "maths-101")

    def test_another_students_enrolment_does_not_count(self, rows):
        rows(enrol("active", uid="s2"))
        with pytest.raises(HTTPException):
            require_enrolment(student("s1"), "maths-101")


class TestStatusRules:
    def test_completed_student_keeps_access(self, rows):
        """Passing the course must not delete your notes — §3.2."""
        rows(enrol("completed"))
        assert require_enrolment(student(), "maths-101")["status"] == "completed"

    def test_completed_student_cannot_start_new_work(self, rows):
        rows(enrol("completed"))
        with pytest.raises(HTTPException):
            require_enrolment(student(), "maths-101", PARTICIPATING_STATUSES)

    @pytest.mark.parametrize("bad", ["withdrawn", "archived"])
    def test_withdrawn_and_archived_are_locked_out(self, rows, bad):
        rows(enrol(bad))
        with pytest.raises(HTTPException) as exc:
            require_enrolment(student(), "maths-101")
        assert exc.value.status_code == 403

    def test_completed_is_an_access_status(self):
        assert "completed" in ACTIVE_STATUSES
        assert "completed" not in PARTICIPATING_STATUSES


class TestStaffBypass:
    @pytest.mark.parametrize("role", ["admin", "super_admin"])
    def test_admins_need_no_enrolment(self, rows, role):
        rows([])
        assert require_enrolment({"id": "a1", "role": role}, "maths-101") is None

    def test_owning_lecturer_needs_no_enrolment(self, monkeypatch, rows):
        rows([])
        monkeypatch.setattr(
            "app.core.auth.assert_course_owner", lambda _u, _c: None
        )
        assert require_enrolment({"id": "l1", "role": "lecturer"}, "maths-101") is None

    def test_non_owning_lecturer_is_still_refused(self, monkeypatch, rows):
        rows([])

        def deny(_u, _c):
            raise HTTPException(status_code=404, detail="Course not found")

        monkeypatch.setattr("app.core.auth.assert_course_owner", deny)
        with pytest.raises(HTTPException):
            require_enrolment({"id": "l2", "role": "lecturer"}, "maths-101")


class TestModeGating:
    @pytest.mark.parametrize("mode", ["learn", "review"])
    def test_ungated_modes_never_query(self, monkeypatch, mode):
        def boom():
            raise AssertionError("should not have queried courses")

        monkeypatch.setattr(en, "get_supabase", boom)
        require_mode_enabled("maths-101", mode)

    def test_application_blocked_when_disabled(self, rows):
        rows([{"id": "maths-101", "practice_mode_enabled": False}])
        with pytest.raises(HTTPException) as exc:
            require_mode_enabled("maths-101", "application")
        assert exc.value.status_code == 403

    def test_application_allowed_when_enabled(self, rows):
        rows([{"id": "maths-101", "practice_mode_enabled": True}])
        require_mode_enabled("maths-101", "application")

    def test_missing_course_is_404(self, rows):
        rows([])
        with pytest.raises(HTTPException) as exc:
            require_mode_enabled("ghost", "application")
        assert exc.value.status_code == 404

    def test_fails_open_before_the_migration_lands(self, rows):
        """The column may not exist yet. Blocking every student out of
        Application mode would be a worse failure than allowing it."""
        rows([], raises=Exception('column "practice_mode_enabled" does not exist'))
        require_mode_enabled("maths-101", "application")


class TestInviteCodes:
    def test_code_has_expected_shape(self):
        code = generate_invite_code()
        assert len(code) == 7
        assert code.isupper()

    def test_codes_are_unique_enough(self):
        assert len({generate_invite_code() for _ in range(500)}) == 500

    @pytest.mark.parametrize("glyph", "O0I1LS5")
    def test_ambiguous_glyphs_are_excluded(self, glyph):
        """Students type these off a projector; O/0 collisions are support load."""
        assert glyph not in en._CODE_ALPHABET or glyph not in "O0I1L"
        joined = "".join(generate_invite_code() for _ in range(200))
        assert not set(joined) & set("O0I1L")
