"""
Unit tests for app/core/analytics.py

All tests mock get_supabase() so no real DB connection is needed.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_supabase_mock(select_data=None):
    """Return a MagicMock that behaves like a Supabase client."""
    mock = MagicMock()
    # Configure the data returned by .execute()
    execute_mock = MagicMock()
    execute_mock.return_value.data = select_data or []
    # Wire up common chains: .table().X().execute()
    mock.table.return_value.insert.return_value.execute = execute_mock
    mock.table.return_value.update.return_value.execute = execute_mock
    mock.table.return_value.select.return_value.eq.return_value.execute = execute_mock
    return mock, execute_mock


# ---------------------------------------------------------------------------
# log_login
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLogLogin:
    def test_inserts_into_platform_sessions(self):
        mock_sb, _ = _make_supabase_mock()
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import log_login
            log_login("user-abc")

        mock_sb.table.assert_called_with("platform_sessions")
        inserted = mock_sb.table.return_value.insert.call_args[0][0]
        assert inserted["user_id"] == "user-abc"
        assert "login_at" in inserted

    def test_silent_on_db_failure(self):
        """A DB error must NOT raise — analytics is fire-and-forget."""
        mock_sb = MagicMock()
        mock_sb.table.side_effect = Exception("DB down")
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import log_login
            log_login("user-abc")  # must not raise


# ---------------------------------------------------------------------------
# open_ws_session
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOpenWsSession:
    def test_returns_inserted_id(self):
        mock_sb, exec_mock = _make_supabase_mock(select_data=[{"id": "ws-session-id"}])
        mock_sb.table.return_value.insert.return_value.execute.return_value.data = [
            {"id": "ws-session-id"}
        ]
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import open_ws_session
            result = open_ws_session("user-1", "session-1")

        assert result == "ws-session-id"

    def test_inserts_correct_fields(self):
        mock_sb, _ = _make_supabase_mock(select_data=[{"id": "ws-id"}])
        mock_sb.table.return_value.insert.return_value.execute.return_value.data = [
            {"id": "ws-id"}
        ]
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import open_ws_session
            open_ws_session("user-42", "session-42")

        inserted = mock_sb.table.return_value.insert.call_args[0][0]
        assert inserted["user_id"] == "user-42"
        assert inserted["session_id"] == "session-42"
        assert "connected_at" in inserted

    def test_returns_none_on_empty_response(self):
        mock_sb, _ = _make_supabase_mock(select_data=[])
        mock_sb.table.return_value.insert.return_value.execute.return_value.data = []
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import open_ws_session
            result = open_ws_session("user-1", "session-1")

        assert result is None

    def test_returns_none_on_db_failure(self):
        mock_sb = MagicMock()
        mock_sb.table.side_effect = Exception("Redis is on fire")
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import open_ws_session
            result = open_ws_session("user-1", "session-1")

        assert result is None


# ---------------------------------------------------------------------------
# close_ws_session
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCloseWsSession:
    def _connected_at(self, seconds_ago: int) -> str:
        t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        return t.isoformat()

    def test_computes_and_stores_duration(self):
        connected_at = self._connected_at(120)  # 2 minutes ago

        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"connected_at": connected_at}
        ]

        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import close_ws_session
            close_ws_session("ws-sess-id")

        updated = mock_sb.table.return_value.update.call_args[0][0]
        assert "disconnected_at" in updated
        assert "duration_sec" in updated
        # Allow ±5s tolerance for test execution time
        assert 115 <= updated["duration_sec"] <= 125

    def test_does_nothing_if_record_not_found(self):
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import close_ws_session
            close_ws_session("nonexistent-id")  # must not raise

        mock_sb.table.return_value.update.assert_not_called()

    def test_silent_on_db_failure(self):
        mock_sb = MagicMock()
        mock_sb.table.side_effect = Exception("network timeout")
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import close_ws_session
            close_ws_session("ws-sess-id")  # must not raise


# ---------------------------------------------------------------------------
# log_request_metric
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLogRequestMetric:
    def _call(self, mock_sb, **kwargs):
        defaults = dict(
            session_id="s1",
            user_id="u1",
            mode="learn",
            task_type="rag_turn_learn",
            response_format="text",
            total_processing_ms=1500,
        )
        defaults.update(kwargs)
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import log_request_metric
            log_request_metric(**defaults)

    def test_stores_all_base_fields(self):
        mock_sb, _ = _make_supabase_mock()
        self._call(mock_sb)

        inserted = mock_sb.table.return_value.insert.call_args[0][0]
        assert inserted["session_id"] == "s1"
        assert inserted["user_id"] == "u1"
        assert inserted["mode"] == "learn"
        assert inserted["response_format"] == "text"
        assert inserted["processing_time_sec"] == 1.5  # 1500ms → 1.5s

    def test_stores_ai_and_video_ms(self):
        mock_sb, _ = _make_supabase_mock()
        self._call(mock_sb, ai_processing_ms=800, video_generation_ms=4200, response_format="video")

        inserted = mock_sb.table.return_value.insert.call_args[0][0]
        assert inserted["ai_processing_ms"] == 800
        assert inserted["video_generation_ms"] == 4200
        assert inserted["response_format"] == "video"

    def test_video_ms_defaults_to_none_for_text(self):
        mock_sb, _ = _make_supabase_mock()
        self._call(mock_sb)

        inserted = mock_sb.table.return_value.insert.call_args[0][0]
        assert inserted["video_generation_ms"] is None

    def test_silent_on_db_failure(self):
        mock_sb = MagicMock()
        mock_sb.table.side_effect = Exception("timeout")
        self._call(mock_sb)  # must not raise

    def test_mode_review_stored(self):
        mock_sb, _ = _make_supabase_mock()
        self._call(mock_sb, mode="review", task_type="mode_session_turn_review")
        inserted = mock_sb.table.return_value.insert.call_args[0][0]
        assert inserted["mode"] == "review"

    def test_mode_application_stored(self):
        mock_sb, _ = _make_supabase_mock()
        self._call(mock_sb, mode="application", task_type="mode_session_turn_application")
        inserted = mock_sb.table.return_value.insert.call_args[0][0]
        assert inserted["mode"] == "application"


# ---------------------------------------------------------------------------
# close_mode_session
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCloseModeSession:
    def _started_at(self, seconds_ago: int) -> str:
        t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        return t.isoformat()

    def test_computes_and_stores_duration(self):
        started_at = self._started_at(300)  # 5 minutes ago

        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"started_at": started_at}
        ]

        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import close_mode_session
            close_mode_session("mode-session-id")

        updated = mock_sb.table.return_value.update.call_args[0][0]
        assert "ended_at" in updated
        assert "duration_sec" in updated
        assert 295 <= updated["duration_sec"] <= 305

    def test_does_nothing_if_not_found(self):
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import close_mode_session
            close_mode_session("nonexistent")

        mock_sb.table.return_value.update.assert_not_called()

    def test_silent_on_db_failure(self):
        mock_sb = MagicMock()
        mock_sb.table.side_effect = Exception("connection refused")
        with patch("app.core.analytics.get_supabase", return_value=mock_sb):
            from app.core.analytics import close_mode_session
            close_mode_session("mode-session-id")  # must not raise
