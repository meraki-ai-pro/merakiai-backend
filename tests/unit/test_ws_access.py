"""F-02: the WebSocket applies the same enrolment / mode gates as REST."""
from fastapi import HTTPException

from app.api.v1.ws import router as ws


def test_no_course_is_refused():
    assert ws._check_course_access("u", "e", None)


def test_withdrawn_student_is_refused(monkeypatch):
    def deny(*a, **k):
        raise HTTPException(status_code=403, detail="You have been withdrawn from this course.")
    monkeypatch.setattr(ws, "require_enrolment", deny)
    assert "withdrawn" in ws._check_course_access("u", "e", "c")


def test_disabled_mode_is_refused(monkeypatch):
    monkeypatch.setattr(ws, "require_enrolment", lambda *a, **k: None)
    def deny(*a, **k):
        raise HTTPException(status_code=403, detail="Application mode is disabled for this course.")
    monkeypatch.setattr(ws, "require_mode_enabled", deny)
    assert "disabled" in ws._check_course_access("u", "e", "c", "application")


def test_enrolled_student_passes(monkeypatch):
    monkeypatch.setattr(ws, "require_enrolment", lambda *a, **k: None)
    monkeypatch.setattr(ws, "require_mode_enabled", lambda *a, **k: None)
    assert ws._check_course_access("u", "e", "c", "review") is None
