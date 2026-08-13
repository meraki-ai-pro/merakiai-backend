"""Role assignment rules for the new 'lecturer' role.

The rule that matters most here is symmetry: admin rights are super-admin
territory when granted AND when revoked. Checking only the incoming role would
let a plain admin demote a super_admin, which is an escalation by removal.
"""

import pytest
from fastapi import HTTPException

from app.api.v1.admin.users import (
    _ASSIGNABLE_BY,
    _PRIVILEGED_ROLES,
    _ROLE_HIERARCHY,
    update_user_role,
)


class Payload:
    def __init__(self, role):
        self.role = role


class FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self._filter = None
        self._update = None

    def select(self, *_a, **_k):
        return self

    def update(self, values):
        self._update = values
        return self

    def eq(self, _col, value):
        self._filter = value
        return self

    def execute(self):
        row = self.store.get(self._filter)
        if self._update is not None:
            if row is None:
                return type("R", (), {"data": []})()
            row.update(self._update)
            return type("R", (), {"data": [row]})()
        return type("R", (), {"data": [row] if row else []})()


class FakeSupabase:
    def __init__(self, users):
        self.users = users

    def table(self, name):
        return FakeTable(self.users, name)


@pytest.fixture
def patch_sb(monkeypatch):
    def _apply(users):
        sb = FakeSupabase(users)
        monkeypatch.setattr("app.api.v1.admin.users.get_supabase", lambda: sb)
        return sb

    return _apply


def actor(role, uid="actor-1"):
    return {"id": uid, "role": role, "email": "a@b.c", "token": "t"}


def call(target_id, new_role, caller):
    return update_user_role(target_id, Payload(new_role), user=caller, _mfa=None)


class TestLecturerIsAssignable:
    def test_lecturer_is_a_known_role(self):
        assert "lecturer" in _ROLE_HIERARCHY

    def test_lecturer_is_not_privileged(self):
        """A lecturer must never satisfy a platform-wide authority check."""
        assert "lecturer" not in _PRIVILEGED_ROLES

    def test_admin_may_promote_a_student_to_lecturer(self, patch_sb):
        patch_sb({"u1": {"role": "user"}})
        out = call("u1", "lecturer", actor("admin"))
        assert out["new_role"] == "lecturer"

    def test_admin_may_demote_a_lecturer(self, patch_sb):
        patch_sb({"u1": {"role": "lecturer"}})
        out = call("u1", "user", actor("admin"))
        assert out["new_role"] == "user"


class TestAdminRightsAreSuperAdminOnly:
    def test_admin_cannot_mint_another_admin(self, patch_sb):
        patch_sb({"u1": {"role": "user"}})
        with pytest.raises(HTTPException) as exc:
            call("u1", "admin", actor("admin"))
        assert exc.value.status_code == 403

    def test_admin_cannot_demote_a_super_admin(self, patch_sb):
        """Escalation by removal — the case a target-only check would miss."""
        patch_sb({"u1": {"role": "super_admin"}})
        with pytest.raises(HTTPException) as exc:
            call("u1", "user", actor("admin"))
        assert exc.value.status_code == 403

    def test_admin_cannot_demote_another_admin(self, patch_sb):
        patch_sb({"u1": {"role": "admin"}})
        with pytest.raises(HTTPException) as exc:
            call("u1", "lecturer", actor("admin"))
        assert exc.value.status_code == 403

    def test_super_admin_may_grant_admin(self, patch_sb):
        patch_sb({"u1": {"role": "user"}})
        assert call("u1", "admin", actor("super_admin"))["new_role"] == "admin"

    def test_super_admin_may_revoke_admin(self, patch_sb):
        patch_sb({"u1": {"role": "admin"}})
        assert call("u1", "user", actor("super_admin"))["new_role"] == "user"


class TestGuardRails:
    def test_unknown_role_rejected(self, patch_sb):
        patch_sb({"u1": {"role": "user"}})
        with pytest.raises(HTTPException) as exc:
            call("u1", "wizard", actor("super_admin"))
        assert exc.value.status_code == 400

    def test_cannot_change_own_role(self, patch_sb):
        patch_sb({"actor-1": {"role": "super_admin"}})
        with pytest.raises(HTTPException) as exc:
            call("actor-1", "user", actor("super_admin"))
        assert exc.value.status_code == 400

    def test_missing_user_is_404(self, patch_sb):
        patch_sb({})
        with pytest.raises(HTTPException) as exc:
            call("ghost", "lecturer", actor("admin"))
        assert exc.value.status_code == 404

    def test_a_lecturer_may_assign_nothing(self):
        assert _ASSIGNABLE_BY.get("lecturer", frozenset()) == frozenset()
