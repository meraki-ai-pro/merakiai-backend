"""Client round 2026-10-14: pin/archive sessions, super-admin account CRUD."""

import pytest
from fastapi import HTTPException

from app.api.v1.admin import users as admin_users
from app.api.v1.sessions import router as sessions
from app.models.models import SessionArchiveUpdate, SessionPinUpdate

SID = "11111111-1111-1111-1111-111111111111"


class Query:
    """Records filters and writes; returns rows from the fake DB."""

    def __init__(self, db, table):
        self.db, self.table, self.filters, self.payload, self.op = db, table, [], None, "select"

    def select(self, *_a, **_k):
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self.filters.append(("is", col, val))
        return self

    @property
    def not_(self):
        outer = self

        class _Not:
            def is_(self, col, val):
                outer.filters.append(("not_is", col, val))
                return outer
        return _Not()

    def order(self, *_a, **_k):
        return self

    def range(self, *_a):
        return self

    def in_(self, *_a):
        return self

    def execute(self):
        self.db.calls.append((self.table, self.op, self.payload, list(self.filters)))
        if self.op in ("update", "insert"):
            rows = self.db.rows.get(self.table, [])
            return type("R", (), {"data": rows if self.op == "update" else [self.payload]})()
        return type("R", (), {"data": self.db.rows.get(self.table, [])})()


class FakeAuthAdmin:
    def __init__(self, fail=None):
        self.created, self.updated, self.fail = [], [], fail

    def create_user(self, attrs):
        if self.fail:
            raise RuntimeError(self.fail)
        self.created.append(attrs)
        return type("Res", (), {"user": type("U", (), {"id": "new-user-id"})()})()

    def update_user_by_id(self, uid, attrs):
        self.updated.append((uid, attrs))


class FakeDB:
    def __init__(self, rows=None, auth_fail=None):
        self.rows, self.calls = rows or {}, []
        self.auth = type("A", (), {"admin": FakeAuthAdmin(auth_fail)})()

    def table(self, name):
        return Query(self, name)

    def rpc(self, *_a, **_k):
        return type("X", (), {"execute": lambda s: None})()

    def writes(self, table):
        return [c for c in self.calls if c[0] == table and c[1] in ("update", "insert")]


STUDENT = {"id": "u1", "role": "user", "token": "t"}
SUPER = {"id": "sa", "role": "super_admin"}
ADMIN = {"id": "ad", "role": "admin"}


# ── Pin / archive ─────────────────────────────────────────────────────────────

class TestPinArchive:
    def test_pin_sets_and_unpin_clears(self, monkeypatch):
        db = FakeDB(rows={"sessions": [{"id": SID}]})
        monkeypatch.setattr(sessions, "get_user_client", lambda _t: db)
        out = sessions.pin_session(SID, SessionPinUpdate(pinned=True), STUDENT)
        assert out["pinned_at"] and db.writes("sessions")[-1][2] == {"pinned_at": out["pinned_at"]}
        sessions.pin_session(SID, SessionPinUpdate(pinned=False), STUDENT)
        assert db.writes("sessions")[-1][2] == {"pinned_at": None}

    def test_archiving_also_unpins_but_restoring_does_not_touch_the_pin(self, monkeypatch):
        db = FakeDB(rows={"sessions": [{"id": SID}]})
        monkeypatch.setattr(sessions, "get_user_client", lambda _t: db)
        sessions.archive_session(SID, SessionArchiveUpdate(archived=True), STUDENT)
        assert db.writes("sessions")[-1][2]["pinned_at"] is None
        sessions.archive_session(SID, SessionArchiveUpdate(archived=False), STUDENT)
        assert db.writes("sessions")[-1][2] == {"archived_at": None}

    def test_someone_elses_session_is_a_404(self, monkeypatch):
        """RLS hides it, so the existence check finds nothing."""
        db = FakeDB(rows={"sessions": []})
        monkeypatch.setattr(sessions, "get_user_client", lambda _t: db)
        with pytest.raises(HTTPException) as e:
            sessions.pin_session(SID, SessionPinUpdate(pinned=True), STUDENT)
        assert e.value.status_code == 404 and not db.writes("sessions")

    def test_sidebar_list_hides_archived_and_archive_page_shows_only_archived(self, monkeypatch):
        db = FakeDB(rows={"sessions": [], "conversations": []})
        monkeypatch.setattr(sessions, "get_user_client", lambda _t: db)
        sessions.list_sessions(user=STUDENT, limit=50, offset=0, archived=False)
        assert ("is", "archived_at", "null") in db.calls[0][3]
        db.calls.clear()
        sessions.list_sessions(user=STUDENT, limit=50, offset=0, archived=True)
        assert ("not_is", "archived_at", "null") in db.calls[0][3]


# ── Super-admin account CRUD ──────────────────────────────────────────────────

def _create(**over):
    return admin_users.UserCreatePayload(**{
        "email": "new.lecturer@ug.edu.gh", "password": "s3cret-pass", "role": "lecturer",
        "first_name": "Ama", "last_name": "Mensah", **over,
    })


@pytest.fixture
def no_audit(monkeypatch):
    seen = []
    monkeypatch.setattr(admin_users.audit, "record", lambda **k: seen.append(k))
    return seen


class TestCreateAccount:
    def test_only_super_admins(self, monkeypatch, no_audit):
        monkeypatch.setattr(admin_users, "get_supabase", lambda: pytest.fail("DB touched"))
        with pytest.raises(HTTPException) as e:
            admin_users.create_user(_create(), None, ADMIN, None)
        assert e.value.status_code == 403

    def test_creates_a_confirmed_account_with_the_role(self, monkeypatch, no_audit):
        db = FakeDB(rows={"users": [{"id": "new-user-id"}]})
        monkeypatch.setattr(admin_users, "get_supabase", lambda: db)
        out = admin_users.create_user(_create(), None, SUPER, None)
        (attrs,) = db.auth.admin.created
        assert attrs["email_confirm"] is True and attrs["password"] == "s3cret-pass"
        assert db.writes("users")[0][2]["role"] == "lecturer"
        assert out["user_id"] == "new-user-id"
        assert "password" not in str(no_audit[0]["new_values"])

    def test_without_the_auth_trigger_the_profile_is_inserted(self, monkeypatch, no_audit):
        db = FakeDB(rows={"users": [], "avatar_voice_bundles": [{"avatar_id": "av1", "is_active": True}]})
        monkeypatch.setattr(admin_users, "get_supabase", lambda: db)
        admin_users.create_user(_create(), None, SUPER, None)
        insert = [c for c in db.writes("users") if c[1] == "insert"][0][2]
        assert insert["avatar_id"] == "av1" and insert["id"] == "new-user-id"

    def test_existing_email_is_a_409(self, monkeypatch, no_audit):
        db = FakeDB(auth_fail="A user with this email address has already been registered")
        monkeypatch.setattr(admin_users, "get_supabase", lambda: db)
        with pytest.raises(HTTPException) as e:
            admin_users.create_user(_create(), None, SUPER, None)
        assert e.value.status_code == 409

    def test_unknown_role_is_rejected(self, monkeypatch, no_audit):
        monkeypatch.setattr(admin_users, "get_supabase", lambda: FakeDB())
        with pytest.raises(HTTPException) as e:
            admin_users.create_user(_create(role="owner"), None, SUPER, None)
        assert e.value.status_code == 400


class TestUpdateAccount:
    def _db(self):
        return FakeDB(rows={"users": [{"id": "u9", "role": "user", "email": "old@ug.edu.gh"}]})

    def test_only_super_admins(self, monkeypatch, no_audit):
        monkeypatch.setattr(admin_users, "get_supabase", lambda: pytest.fail("DB touched"))
        with pytest.raises(HTTPException) as e:
            admin_users.update_user("u9", admin_users.UserUpdatePayload(first_name="X"), None, ADMIN, None)
        assert e.value.status_code == 403

    def test_email_and_password_go_to_auth_details_to_the_profile(self, monkeypatch, no_audit):
        db = self._db()
        monkeypatch.setattr(admin_users, "get_supabase", lambda: db)
        admin_users.update_user(
            "u9", admin_users.UserUpdatePayload(email="new@ug.edu.gh", password="another-pass", country="Ghana"),
            None, SUPER, None,
        )
        (uid, attrs), = db.auth.admin.updated
        assert uid == "u9" and attrs == {"email": "new@ug.edu.gh", "password": "another-pass"}
        assert db.writes("users")[-1][2] == {"country": "Ghana", "email": "new@ug.edu.gh"}
        # The audit says a reset happened; it never stores the password.
        assert no_audit[0]["new_values"]["password"] == "reset"

    def test_details_only_never_touches_auth(self, monkeypatch, no_audit):
        db = self._db()
        monkeypatch.setattr(admin_users, "get_supabase", lambda: db)
        admin_users.update_user("u9", admin_users.UserUpdatePayload(last_name="Owusu"), None, SUPER, None)
        assert db.auth.admin.updated == []

    def test_empty_update_is_rejected(self, monkeypatch, no_audit):
        monkeypatch.setattr(admin_users, "get_supabase", self._db)
        with pytest.raises(HTTPException) as e:
            admin_users.update_user("u9", admin_users.UserUpdatePayload(), None, SUPER, None)
        assert e.value.status_code == 400


# ── Invite redemption (sql/020) ───────────────────────────────────────────────

class TestInviteRedemption:
    def test_server_side_function_gets_the_verified_student_id(self, monkeypatch):
        """The student id comes from the JWT the backend verified, never from
        the request body, and the call uses the service role — the function is
        not callable by signed-in users over REST."""
        from app.core import enrolment
        import app.db.supabase as supa

        calls = []

        class RPC:
            def __init__(self, name, args):
                calls.append((name, args))

            def execute(self):
                return type("R", (), {"data": {"course_id": "c1", "student_id": "stu-1"}})()

        monkeypatch.setattr(supa, "get_supabase", lambda: type("S", (), {"rpc": lambda s, n, a: RPC(n, a)})())
        out = enrolment.redeem_code("  abc123 ", "stu-1")
        assert calls == [("redeem_invite_code_for", {"p_student_id": "stu-1", "p_code": "abc123"})]
        assert out["course_id"] == "c1"

    def test_function_messages_reach_the_student(self, monkeypatch):
        from app.core import enrolment
        import app.db.supabase as supa

        def boom(*_a):
            raise RuntimeError("{'message': 'This invite code has expired', 'code': 'P0002'}")

        monkeypatch.setattr(supa, "get_supabase", lambda: type("S", (), {"rpc": lambda s, n, a: type("X", (), {"execute": boom})()})())
        with pytest.raises(HTTPException) as e:
            enrolment.redeem_code("abc", "stu-1")
        assert e.value.status_code == 400 and "expired" in e.value.detail
