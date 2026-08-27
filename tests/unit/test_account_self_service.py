"""Profile editing and password change, for every role.

Students, lecturers and admins all reach the same two endpoints — there is no
per-role copy of this, deliberately, because three implementations of "change
your password" is three places for the current-password check to be missing
from one of them.

The check under test: a valid access token alone must not be enough to take an
account over. Before this change /auth/update-password took only a new
password, so anyone holding a token for its remaining lifetime could set a
password the owner did not know and keep the account permanently.
"""

import ast
import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1 import auth as auth_module
from app.api.v1.users import router as users_module
from app.models.models import UpdatePasswordPayload, UpdateProfilePayload

BACKEND = Path(__file__).resolve().parents[2]


class TestPasswordChangeRequiresTheCurrentPassword:
    def test_current_password_is_mandatory(self):
        with pytest.raises(ValidationError):
            UpdatePasswordPayload(new_password="a-new-password")

    def test_payload_accepts_both(self):
        payload = UpdatePasswordPayload(
            current_password="old-one", new_password="a-new-password"
        )
        assert payload.current_password == "old-one"

    def test_new_password_has_a_minimum_length(self):
        with pytest.raises(ValidationError):
            UpdatePasswordPayload(current_password="x", new_password="short")

    def test_the_handler_reauthenticates_before_updating(self):
        """A structural check, because the failure mode is silent: an
        update_user_by_id call with no preceding sign-in still 200s, and the
        endpoint would look like it works."""
        source = inspect.getsource(auth_module.update_password)
        assert "sign_in_with_password" in source
        assert source.index("sign_in_with_password") < source.index("update_user_by_id")

    def test_a_wrong_current_password_is_a_401(self, monkeypatch):
        class Boom:
            class auth:
                @staticmethod
                def sign_in_with_password(_payload):
                    raise RuntimeError("Invalid login credentials")

        monkeypatch.setattr(auth_module, "get_supabase_anon", lambda: Boom())

        with pytest.raises(HTTPException) as exc:
            auth_module.update_password(
                UpdatePasswordPayload(current_password="wrong", new_password="new-password-1"),
                user={"id": "u1", "email": "ama@ug.edu.gh"},
            )
        assert exc.value.status_code == 401

    def test_reusing_the_same_password_is_refused_without_a_round_trip(self, monkeypatch):
        def explode():
            raise AssertionError("must not reach the auth provider")

        monkeypatch.setattr(auth_module, "get_supabase_anon", explode)

        with pytest.raises(HTTPException) as exc:
            auth_module.update_password(
                UpdatePasswordPayload(current_password="same-password", new_password="same-password"),
                user={"id": "u1", "email": "ama@ug.edu.gh"},
            )
        assert exc.value.status_code == 400


class TestProfileEditing:
    def test_role_cannot_be_set_by_the_owner(self):
        """Self-service role assignment is a straight privilege escalation.
        Pydantic drops unknown fields, so the guarantee is that `role` is not a
        field — not that the route filters it out."""
        assert "role" not in UpdateProfilePayload.model_fields

    def test_email_cannot_be_changed_here(self):
        """Email is an auth identity; changing it needs the verification round
        trip Supabase owns."""
        assert "email" not in UpdateProfilePayload.model_fields

    def test_editable_fields(self):
        assert set(UpdateProfilePayload.model_fields) == {
            "first_name", "last_name", "university_name", "region", "country",
        }

    def test_partial_updates_do_not_blank_the_rest(self):
        """A form that posts only the changed field must not null the others."""
        payload = UpdateProfilePayload(first_name="Ama")
        assert payload.model_dump(exclude_unset=True) == {"first_name": "Ama"}

    def test_the_route_uses_exclude_unset(self):
        source = inspect.getsource(users_module.update_me)
        assert "exclude_unset=True" in source

    def test_the_route_writes_through_the_callers_own_token(self):
        """Service-role would happily update any row. RLS is the enforcement
        that stops a threaded-through id becoming a cross-account write."""
        source = inspect.getsource(users_module.update_me)
        assert "get_user_client" in source
        assert "get_supabase()" not in source

    def test_profile_read_returns_the_name_fields(self):
        """They were missing from GET /users/me, so a profile form had nothing
        to prefill from and would have blanked the user's own name."""
        row = {
            "id": "u1", "email": "ama@ug.edu.gh", "role": "user",
            "first_name": "Ama", "last_name": "Mensah",
            "university_name": "University of Ghana",
            "region": "Greater Accra", "country": "Ghana",
        }
        view = users_module._profile_view(row)
        assert view["first_name"] == "Ama"
        assert view["university_name"] == "University of Ghana"
        assert view["country"] == "Ghana"

    def test_profile_read_is_a_whitelist(self):
        """`users` carries deletion-lifecycle columns and internal flags. A
        pass-through would make every one of them an API contract."""
        view = users_module._profile_view({
            "id": "u1", "email": "a@b.c", "role": "user",
            "deletion_requested_at": "2026-01-01", "internal_notes": "secret",
        })
        assert "deletion_requested_at" not in view
        assert "internal_notes" not in view


class TestEveryRoleUsesTheSameEndpoints:
    def test_there_is_exactly_one_password_change_route(self):
        """Three role-specific copies is three places for the
        current-password check to be missing from one."""
        tree = ast.parse((BACKEND / "app/api/v1/auth.py").read_text(encoding="utf-8"))
        paths = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and dec.args:
                    arg = dec.args[0]
                    if isinstance(arg, ast.Constant) and "password" in str(arg.value):
                        paths.append(arg.value)

        assert sorted(paths) == ["/forgot-password", "/reset-password", "/update-password"]

    def test_profile_editing_is_not_duplicated_per_role(self):
        for area in ("lecturer", "admin"):
            for path in (BACKEND / "app/api/v1" / area).glob("*.py"):
                source = path.read_text(encoding="utf-8")
                assert "UpdateProfilePayload" not in source, (
                    f"{path.name} has its own profile editing; PATCH /users/me "
                    "is the one implementation every role shares."
                )
