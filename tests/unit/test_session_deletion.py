"""Owner-scoped permanent deletion for student sessions."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.api.v1.sessions import router as sessions


class Result:
    def __init__(self, data=None):
        self.data = data or []


class Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self.action = "select"
        self.filters = {}
        self.values = None

    def select(self, *_args, **_kwargs):
        return self

    def update(self, values):
        self.action = "update"
        self.values = values
        return self

    def delete(self):
        self.action = "delete"
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def execute(self):
        if self.action == "select":
            rows = self.db.rows.get(self.table, [])
            return Result([
                row for row in rows
                if all(row.get(key) == value for key, value in self.filters.items())
            ])
        self.db.operations.append((self.action, self.table, self.values, self.filters))
        return Result([self.filters])


class Bucket:
    def __init__(self, db):
        self.db = db

    def list(self, folder):
        self.db.listed_folder = folder
        return [{"name": "first.png"}, {"name": "second.webp"}]

    def remove(self, paths):
        self.db.removed_paths = paths


class Storage:
    def __init__(self, db):
        self.db = db

    def from_(self, bucket):
        self.db.bucket = bucket
        return Bucket(self.db)


class FakeSupabase:
    def __init__(self, **rows):
        self.rows = rows
        self.operations = []
        self.storage = Storage(self)
        self.bucket = None
        self.listed_folder = None
        self.removed_paths = []

    def table(self, name):
        return Query(self, name)


def caller(user_id="student-1"):
    return {"id": user_id, "token": "jwt"}


def test_delete_session_rejects_invalid_id_before_database_access(monkeypatch):
    monkeypatch.setattr(sessions, "get_user_client", lambda _token: pytest.fail("DB called"))
    with pytest.raises(HTTPException) as exc:
        sessions.delete_session("not-a-uuid", caller())
    assert exc.value.status_code == 400


def test_delete_session_returns_404_for_missing_or_unowned_session(monkeypatch):
    sid = str(uuid.uuid4())
    user_db = FakeSupabase(sessions=[{"id": sid, "user_id": "somebody-else"}])
    monkeypatch.setattr(sessions, "get_user_client", lambda _token: user_db)
    monkeypatch.setattr(sessions, "get_supabase", lambda: pytest.fail("service role called"))

    with pytest.raises(HTTPException) as exc:
        sessions.delete_session(sid, caller())
    assert exc.value.status_code == 404


def test_delete_session_cleans_uploads_children_and_parent(monkeypatch):
    sid = str(uuid.uuid4())
    user_db = FakeSupabase(sessions=[{"id": sid, "user_id": "student-1"}])
    service_db = FakeSupabase()
    monkeypatch.setattr(sessions, "get_user_client", lambda _token: user_db)
    monkeypatch.setattr(sessions, "get_supabase", lambda: service_db)

    result = sessions.delete_session(sid, caller())

    assert result == {
        "session_id": sid,
        "status": "deleted",
        "storage_objects_deleted": 2,
    }
    assert service_db.bucket == sessions.STUDENT_UPLOADS_BUCKET
    assert service_db.listed_folder == f"student-1/{sid}"
    assert service_db.removed_paths == [
        f"student-1/{sid}/first.png",
        f"student-1/{sid}/second.webp",
    ]

    actions = [(action, table) for action, table, _values, _filters in service_db.operations]
    assert actions[:3] == [
        ("update", "user_feedback"),
        ("update", "feedback_responses"),
        ("update", "events"),
    ]
    assert actions[-1] == ("delete", "sessions")
    assert actions.index(("delete", "session_state")) < actions.index(("delete", "mode_sessions"))
    assert actions.index(("delete", "conversations")) < actions.index(("delete", "sessions"))
