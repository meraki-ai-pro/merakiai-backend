"""Course-scoped lecturer authority.

Passing lecturer_guard proves only that the caller is a lecturer. Every route
touching a named course must also pass assert_course_owner(), or one lecturer
reaches another lecturer's course.
"""

import pytest
from fastapi import HTTPException

from app.core import auth
from app.core.auth import ADMIN_ROLES, LECTURER_ROLES, assert_course_owner


class FakeTable:
    def __init__(self, rows):
        self.rows = rows
        self._id = None

    def select(self, *_a, **_k):
        return self

    def eq(self, _col, value):
        self._id = value
        return self

    def execute(self):
        row = self.rows.get(self._id)
        return type("R", (), {"data": [row] if row else []})()


class FakeSupabase:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return FakeTable(self.rows)


@pytest.fixture
def courses(monkeypatch):
    def _apply(rows):
        monkeypatch.setattr(auth, "get_supabase", lambda: FakeSupabase(rows))

    return _apply


def user(role, uid):
    return {"id": uid, "role": role, "email": "a@b.c", "token": "t"}


class TestOwnership:
    def test_owner_passes(self, courses):
        courses({"maths-101": {"id": "maths-101", "owner_id": "lec-1"}})
        assert_course_owner(user("lecturer", "lec-1"), "maths-101")

    def test_non_owner_is_rejected(self, courses):
        courses({"maths-101": {"id": "maths-101", "owner_id": "lec-1"}})
        with pytest.raises(HTTPException):
            assert_course_owner(user("lecturer", "lec-2"), "maths-101")

    def test_unowned_course_is_rejected(self, courses):
        """Legacy courses predate ownership; a lecturer must not inherit them."""
        courses({"froth": {"id": "froth", "owner_id": None}})
        with pytest.raises(HTTPException):
            assert_course_owner(user("lecturer", "lec-1"), "froth")


class TestEnumerationResistance:
    def test_someone_elses_course_is_404_not_403(self, courses):
        """403 would confirm the id exists, letting a lecturer probe for
        other lecturers' course ids."""
        courses({"maths-101": {"id": "maths-101", "owner_id": "lec-1"}})
        with pytest.raises(HTTPException) as exc:
            assert_course_owner(user("lecturer", "lec-2"), "maths-101")
        assert exc.value.status_code == 404

    def test_missing_course_is_also_404(self, courses):
        courses({})
        with pytest.raises(HTTPException) as exc:
            assert_course_owner(user("lecturer", "lec-1"), "nope")
        assert exc.value.status_code == 404


class TestAdminOverride:
    @pytest.mark.parametrize("role", ADMIN_ROLES)
    def test_admins_bypass_ownership(self, courses, role):
        courses({"maths-101": {"id": "maths-101", "owner_id": "lec-1"}})
        assert_course_owner(user(role, "admin-1"), "maths-101")

    def test_admin_override_does_not_query_at_all(self, monkeypatch):
        """Admins short-circuit before the lookup, so support access still
        works if the course row is unreadable."""
        def boom():
            raise AssertionError("should not have queried courses")

        monkeypatch.setattr(auth, "get_supabase", boom)
        assert_course_owner(user("admin", "a1"), "anything")


class TestRoleSets:
    def test_lecturer_may_reach_lecturer_routes(self):
        assert "lecturer" in LECTURER_ROLES

    def test_lecturer_is_not_an_admin_role(self):
        assert "lecturer" not in ADMIN_ROLES

    def test_student_reaches_neither(self):
        assert "user" not in LECTURER_ROLES
        assert "user" not in ADMIN_ROLES
