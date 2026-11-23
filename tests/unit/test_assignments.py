"""Assigned activities: derived completion, and the study lock."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.core import assignments


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_):
        return self

    def eq(self, col, val):
        return FakeQuery([r for r in self.rows if r.get(col) == val])

    def in_(self, col, vals):
        return FakeQuery([r for r in self.rows if r.get(col) in vals])

    def order(self, *_, **__):
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


def install(monkeypatch, **tables):
    db = type("DB", (), {"table": lambda self, name: FakeQuery(tables.get(name, []))})()
    monkeypatch.setattr(assignments, "get_supabase", lambda: db)


ISO = lambda d: d.isoformat()  # noqa: E731
NOW = datetime.now(timezone.utc)


def lesson(sid, required=True):
    return {"id": f"a-{sid}", "kind": "lesson", "title": "Limits", "required": required,
            "session_id": sid, "assessment_id": None, "created_at": "x",
            "student_id": "stu", "course_id": "c1"}


def paper(aid, required=True):
    return {"id": f"a-{aid}", "kind": "paper", "title": "Practice", "required": required,
            "session_id": None, "assessment_id": aid, "created_at": "x",
            "student_id": "stu", "course_id": "c1"}


def test_lesson_is_done_once_the_student_replies(monkeypatch):
    install(monkeypatch, student_assignments=[lesson("s1")],
            conversations=[{"session_id": "s1", "user_input": "(system) lecturer mini-lesson"}])
    assert assignments.for_student("stu", "c1")[0]["status"] == "todo"
    install(monkeypatch, student_assignments=[lesson("s1")],
            conversations=[{"session_id": "s1", "user_input": "(system) lecturer mini-lesson"},
                           {"session_id": "s1", "user_input": "is it 4?"}])
    assert assignments.for_student("stu", "c1")[0]["status"] == "done"


def test_unpublished_paper_is_hidden_and_locks_nothing(monkeypatch):
    install(monkeypatch, student_assignments=[paper("p1")],
            assessments=[{"id": "p1", "is_published": False}])
    assert assignments.for_student("stu", "c1") == []
    assignments.require_unlocked("stu", "c1")  # no raise


def test_required_open_paper_locks_until_submitted(monkeypatch):
    install(monkeypatch, student_assignments=[paper("p1")],
            assessments=[{"id": "p1", "is_published": True}])
    with pytest.raises(HTTPException) as e:
        assignments.require_unlocked("stu", "c1")
    assert e.value.status_code == 423
    install(monkeypatch, student_assignments=[paper("p1")],
            assessments=[{"id": "p1", "is_published": True}],
            assessment_attempts=[{"assessment_id": "p1", "student_id": "stu"}])
    assignments.require_unlocked("stu", "c1")


def test_closed_or_not_yet_open_papers_do_not_lock(monkeypatch):
    install(monkeypatch, student_assignments=[paper("p1"), paper("p2")], assessments=[
        {"id": "p1", "is_published": True, "closes_at": ISO(NOW - timedelta(days=1))},
        {"id": "p2", "is_published": True, "opens_at": ISO(NOW + timedelta(days=3))},
    ])
    statuses = {a["assessment_id"]: a["status"] for a in assignments.for_student("stu", "c1")}
    assert statuses == {"p1": "closed", "p2": "todo"}
    assignments.require_unlocked("stu", "c1")


def test_the_assigned_lesson_itself_stays_open(monkeypatch):
    install(monkeypatch, student_assignments=[lesson("s1")], conversations=[])
    assignments.require_unlocked("stu", "c1", "s1")  # its own session: allowed
    with pytest.raises(HTTPException):
        assignments.require_unlocked("stu", "c1", "other-session")
    with pytest.raises(HTTPException):
        assignments.require_unlocked("stu", "c1")  # new session


def test_optional_activities_never_lock(monkeypatch):
    install(monkeypatch, student_assignments=[lesson("s1", required=False)], conversations=[])
    assignments.require_unlocked("stu", "c1")
