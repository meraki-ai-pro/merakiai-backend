"""F-05/F-06/F-09/F-03 regression tests."""
import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.v1 import lifecycle as lc_api
from app.api.v1.lecturer.data_center import safe_cell
from app.api.v1.video import router as video
from app.core import lifecycle


def _sb_with_role(role):
    sb = MagicMock()
    sb.table().select().eq().execute.return_value.data = [{"role": role}] if role else []
    return sb


def test_plain_admin_cannot_manage_admin_tiers(monkeypatch):
    for target in ("admin", "super_admin"):
        monkeypatch.setattr(lc_api, "get_supabase", lambda t=target: _sb_with_role(t))
        with pytest.raises(HTTPException) as e:
            lc_api._require_may_manage({"role": "admin"}, "x")
        assert e.value.status_code == 403


def test_super_admin_may_manage_admin_but_not_delete_super(monkeypatch):
    monkeypatch.setattr(lc_api, "get_supabase", lambda: _sb_with_role("admin"))
    lc_api._require_may_manage({"role": "super_admin"}, "x", deleting=True)
    monkeypatch.setattr(lc_api, "get_supabase", lambda: _sb_with_role("super_admin"))
    with pytest.raises(HTTPException) as e:
        lc_api._require_may_manage({"role": "super_admin"}, "x", deleting=True)
    assert e.value.status_code == 400


def test_soft_delete_bans_the_auth_user(monkeypatch):
    sb = MagicMock()
    sb.table().select().eq().execute.return_value.data = [{"id": "u", "email": "a@b.c"}]
    monkeypatch.setattr(lifecycle, "get_supabase", lambda: sb)
    monkeypatch.setattr(lifecycle, "_record", lambda **k: None)
    lifecycle.soft_delete("u", requested_by="a", reason="r")
    sb.auth.admin.update_user_by_id.assert_called_with("u", {"ban_duration": lifecycle.BAN_FOREVER})


def test_restore_unbans(monkeypatch):
    sb = MagicMock()
    sb.table().update().eq().execute.return_value.data = [{"id": "u"}]
    sb.table().select().eq().execute.return_value.data = [{"deleted_at": "2026-01-01"}]
    monkeypatch.setattr(lifecycle, "get_supabase", lambda: sb)
    lifecycle.restore("u")
    sb.auth.admin.update_user_by_id.assert_called_with("u", {"ban_duration": "none"})


@pytest.mark.parametrize("v", ["=HYPERLINK(\"http://x\")", "+1", "-1+1", "@SUM(A1)"])
def test_safe_cell_neutralises_formulas(v):
    assert safe_cell(v).startswith("'")


def test_video_stream_requires_session_owner(monkeypatch):
    sb = MagicMock()
    async def fake_exec():
        r = MagicMock(); r.data = [{"user_id": "someone-else"}]; return r
    sb.table().select().eq().execute = fake_exec
    async def get_sb(): return sb
    monkeypatch.setattr(video, "get_async_supabase", get_sb)
    with pytest.raises(HTTPException) as e:
        asyncio.run(video._assert_session_owner("s", {"id": "me"}))
    assert e.value.status_code == 404


def test_stream_id_must_match_cached_stream():
    video._require_stream_id({"stream_id": "a"}, "a")
    with pytest.raises(HTTPException) as e:
        video._require_stream_id({"stream_id": "a"}, "b")
    assert e.value.status_code == 404
