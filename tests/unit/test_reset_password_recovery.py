"""F-04: /auth/reset-password must only accept recovery-link sessions."""
from unittest.mock import patch

from app.api.v1 import auth as auth_mod


def _claims(amr):
    return patch.object(auth_mod, "_decode_access_token", return_value={"amr": amr})


def test_password_login_token_is_not_a_recovery_session():
    with _claims([{"method": "password", "timestamp": 1}]):
        assert auth_mod._is_recovery_session("t") is False


def test_recovery_and_otp_tokens_are_accepted():
    with _claims([{"method": "recovery", "timestamp": 1}]):
        assert auth_mod._is_recovery_session("t") is True
    with _claims([{"method": "otp", "timestamp": 1}]):
        assert auth_mod._is_recovery_session("t") is True


def test_missing_amr_or_bad_token_is_rejected():
    with _claims(None):
        assert auth_mod._is_recovery_session("t") is False
    with patch.object(auth_mod, "_decode_access_token", side_effect=ValueError):
        assert auth_mod._is_recovery_session("t") is False
