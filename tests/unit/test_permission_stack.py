"""The permission stack as wired into the request path.

test_enrolment.py covers the helpers in isolation. This covers the parts that
are easy to get wrong at the call site: lazy role resolution (auth_guard does
not carry a role), and the choice of status set at each entry point.

Ref: Meraki_AI_Student_Permission_Checks §3.1, §3.2
"""

import pytest
from fastapi import HTTPException

from app.core import enrolment as en
from app.core.enrolment import (
    ACTIVE_STATUSES,
    PARTICIPATING_STATUSES,
    list_enrolled_course_ids,
    require_enrolment,
)


class FakeTable:
    def __init__(self, tables, name, counter):
        self.tables = tables
        self.name = name
        self.counter = counter
        self.filters = {}
        self.isin = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, col, value):
        self.filters[col] = value
        return self

    def in_(self, col, values):
        self.isin[col] = values
        return self

    def execute(self):
        self.counter[self.name] = self.counter.get(self.name, 0) + 1
        out = [
            r for r in self.tables.get(self.name, [])
            if all(r.get(k) == v for k, v in self.filters.items())
            and all(r.get(k) in v for k, v in self.isin.items())
        ]
        return type("R", (), {"data": out})()


class FakeSupabase:
    def __init__(self, tables, counter, failing_table=None):
        self.tables = tables
        self.counter = counter
        self.failing_table = failing_table

    def table(self, name):
        if name == self.failing_table:
            raise RuntimeError(f"connection reset reading {name}")
        return FakeTable(self.tables, name, self.counter)


@pytest.fixture
def db(monkeypatch):
    counter: dict[str, int] = {}

    def _apply(failing_table=None, **tables):
        monkeypatch.setattr(
            en, "get_supabase", lambda: FakeSupabase(tables, counter, failing_table)
        )
        return counter

    return _apply


def caller(uid="s1"):
    """What auth_guard actually returns — note the absence of a role."""
    return {"id": uid, "email": "s@x.c", "token": "t"}


def enrolment(status_, uid="s1", course="maths-101"):
    return {"id": "e1", "course_id": course, "student_id": uid, "status": status_}


class TestLazyRoleResolution:
    def test_enrolled_student_costs_no_role_lookup(self, db):
        """The hot path must stay at one query — auth_guard omits the role
        precisely so every turn does not pay for a users lookup."""
        counter = db(
            enrolments=[enrolment("active")],
            users=[{"id": "s1", "role": "user"}],
        )
        require_enrolment(caller(), "maths-101")
        assert counter.get("users") is None

    def test_role_is_looked_up_only_when_enrolment_fails(self, db):
        counter = db(enrolments=[], users=[{"id": "a1", "role": "admin"}])
        assert require_enrolment(caller("a1"), "maths-101") is None
        assert counter["users"] == 1

    def test_admin_without_enrolment_passes(self, db):
        db(enrolments=[], users=[{"id": "a1", "role": "super_admin"}])
        assert require_enrolment(caller("a1"), "maths-101") is None

    def test_unknown_user_is_treated_as_a_student(self, db):
        db(enrolments=[], users=[])
        with pytest.raises(HTTPException) as exc:
            require_enrolment(caller("ghost"), "maths-101")
        assert exc.value.status_code == 403

    def test_role_lookup_failure_does_not_grant_access(self, db):
        """A transient error reading the role must fall back to 'user', never
        to staff — a DB blip must not become an authorisation bypass."""
        db(failing_table="users", enrolments=[])
        with pytest.raises(HTTPException) as exc:
            require_enrolment(caller(), "maths-101")
        assert exc.value.status_code == 403

    def test_enrolment_lookup_failure_fails_closed(self, db):
        """An unreadable enrolments table surfaces as an error, not as access.
        Loud 500 beats silent admission."""
        db(failing_table="enrolments", users=[{"id": "s1", "role": "user"}])
        with pytest.raises(Exception) as exc:
            require_enrolment(caller(), "maths-101")
        assert not isinstance(exc.value, HTTPException) or exc.value.status_code >= 400

    def test_supplied_role_is_trusted_without_a_query(self, db):
        counter = db(enrolments=[], users=[{"id": "a1", "role": "user"}])
        user = {**caller("a1"), "role": "admin"}
        assert require_enrolment(user, "maths-101") is None
        assert counter.get("users") is None


class TestStatusSetsPerEntryPoint:
    def test_completed_student_cannot_start_a_session(self, db):
        """POST /sessions/ uses PARTICIPATING_STATUSES — new work needs an
        active enrolment."""
        db(enrolments=[enrolment("completed")], users=[{"id": "s1", "role": "user"}])
        with pytest.raises(HTTPException):
            require_enrolment(caller(), "maths-101", PARTICIPATING_STATUSES)

    def test_completed_student_may_still_take_a_turn(self, db):
        """Turn paths use ACTIVE_STATUSES so a student whose enrolment
        completes mid-set can finish it."""
        db(enrolments=[enrolment("completed")], users=[{"id": "s1", "role": "user"}])
        assert require_enrolment(caller(), "maths-101", ACTIVE_STATUSES)["status"] == "completed"

    def test_withdrawn_student_is_stopped_on_the_next_turn(self, db):
        db(enrolments=[enrolment("withdrawn")], users=[{"id": "s1", "role": "user"}])
        with pytest.raises(HTTPException) as exc:
            require_enrolment(caller(), "maths-101", ACTIVE_STATUSES)
        assert "withdrawn" in exc.value.detail.lower()


class TestCourseListing:
    def test_lists_only_enrolled_courses(self, db):
        db(enrolments=[
            enrolment("active", course="maths-101"),
            enrolment("completed", course="stats-201"),
            enrolment("withdrawn", course="chem-101"),
        ])
        ids = list_enrolled_course_ids("s1")
        assert set(ids) == {"maths-101", "stats-201"}

    def test_withdrawn_courses_are_excluded(self, db):
        db(enrolments=[enrolment("withdrawn", course="chem-101")])
        assert list_enrolled_course_ids("s1") == []

    def test_no_enrolments_returns_empty(self, db):
        db(enrolments=[])
        assert list_enrolled_course_ids("s1") == []
