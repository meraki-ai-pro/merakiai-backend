"""hCaptcha: the backend forwards the token; Supabase Auth verifies it."""

import pytest
from fastapi import HTTPException

from app.api.v1 import auth
from app.models.models import ForgotPasswordPayload, LoginPayload, SignUpPayload, UpdatePasswordPayload


class FakeAuth:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def _record(self, name, *args):
        self.calls.append((name, args))
        if self.error:
            raise RuntimeError(self.error)

    def sign_in_with_password(self, creds):
        self._record("sign_in", creds)
        user = type("U", (), {"id": "u1", "email": "a@ug.edu.gh", "user_metadata": {}})()
        session = type("S", (), {"access_token": "t", "refresh_token": "r", "expires_in": 3600})()
        return type("R", (), {"user": user, "session": session})()

    def sign_up(self, creds):
        self._record("sign_up", creds)
        return type("R", (), {"user": None, "session": None})()

    def reset_password_for_email(self, email, options):
        self._record("reset", email, options)


@pytest.fixture
def fake(monkeypatch):
    holder = {"auth": FakeAuth()}
    monkeypatch.setattr(auth, "get_supabase_anon", lambda: type("C", (), {"auth": holder["auth"]})())
    return holder


def test_login_forwards_the_token(fake, monkeypatch):
    monkeypatch.setattr("app.core.analytics.log_login", lambda *_: None)
    auth.login(LoginPayload(email="a@ug.edu.gh", password="pw", captcha_token="tok"), None)
    (_, (creds,)), = fake["auth"].calls
    assert creds["options"]["captcha_token"] == "tok"


def test_signup_forwards_the_token(fake):
    auth.signup(SignUpPayload(email="a@ug.edu.gh", password="password1", first_name="A", last_name="B",
                              university_name="UG", region="GA", country="Ghana", captcha_token="tok"), None)
    (_, (creds,)), = fake["auth"].calls
    assert creds["options"]["captcha_token"] == "tok"


def test_forgot_password_forwards_the_token(fake):
    auth.forgot_password(ForgotPasswordPayload(email="a@ug.edu.gh", captcha_token="tok"), None)
    (_, (_email, options)), = fake["auth"].calls
    assert options["captcha_token"] == "tok"


def test_no_token_still_works_before_captcha_is_enabled(fake, monkeypatch):
    """No site key configured: the form sends nothing and nothing changes."""
    monkeypatch.setattr("app.core.analytics.log_login", lambda *_: None)
    out = auth.login(LoginPayload(email="a@ug.edu.gh", password="pw"), None)
    assert out["access_token"] == "t"


def test_captcha_rejection_is_not_reported_as_a_wrong_password(fake):
    fake["auth"] = FakeAuth(error="captcha protection: request disallowed (no captcha response)")
    with pytest.raises(HTTPException) as e:
        auth.login(LoginPayload(email="a@ug.edu.gh", password="pw"), None)
    assert e.value.status_code == 400 and "captcha" in e.value.detail.lower()


def test_wrong_password_is_still_a_401(fake):
    fake["auth"] = FakeAuth(error="Invalid login credentials")
    with pytest.raises(HTTPException) as e:
        auth.login(LoginPayload(email="a@ug.edu.gh", password="pw"), None)
    assert e.value.status_code == 401


def test_password_change_reauth_forwards_the_token(fake, monkeypatch):
    monkeypatch.setattr(auth, "get_supabase", lambda: type("C", (), {
        "auth": type("A", (), {"admin": type("Ad", (), {"update_user_by_id": lambda *a, **k: None})()})()
    })())
    auth.update_password(
        UpdatePasswordPayload(current_password="old-pass", new_password="new-pass-1", captcha_token="tok"),
        {"id": "u1", "email": "a@ug.edu.gh"}, None,
    )
    (_, (creds,)), = fake["auth"].calls
    assert creds["options"]["captcha_token"] == "tok"
