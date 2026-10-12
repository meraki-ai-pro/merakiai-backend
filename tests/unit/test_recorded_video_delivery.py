"""Video mode produces a clip that the answer bubble can replay."""

import asyncio
from types import SimpleNamespace

from app.media import video_service


class _Table:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class _Db:
    def table(self, name):
        rows = {
            "users": [{
                "avatar_id": "ava-1",
                "voice_id": "voice-1",
                "avatar_gender": "female",
                "voice_gender": "female",
            }],
            "avatar_voice_bundles": [{
                "did_presenter_id": "presenter-1",
                "is_active": True,
                "avatar_gender": "female",
                "voice_gender": "female",
                "voice_id": "voice-1",
            }],
        }[name]
        return _Table(rows)


def test_video_mode_returns_a_recorded_clip(monkeypatch):
    monkeypatch.setattr(video_service, "get_supabase", lambda: _Db())
    monkeypatch.setattr(video_service, "clean_for_tts", lambda text: text)
    monkeypatch.setattr(video_service, "tts_to_mp3_bytes", lambda *_args, **_kwargs: b"mp3")
    monkeypatch.setattr(
        video_service,
        "upload_audio_and_get_url",
        lambda *_args: "https://media.example/audio.mp3",
    )
    monkeypatch.setattr(video_service, "_PUBLIC_BASE_URL", "")
    monkeypatch.setattr(
        video_service,
        "create_clip_from_audio",
        lambda **_kwargs: {"result_url": "https://media.example/video.mp4"},
    )

    result = asyncio.run(video_service.maybe_generate_video(
        text="A short explanation",
        response_format="video",
        user_id="user-1",
        session_id="session-1",
        mode="learn",
    ))

    assert result["response_format"] == "video"
    assert result["video_url"] == "https://media.example/video.mp4"
    assert result["subtitles_url"] == ""
    assert result["source"] == "did_clips"
    assert "streaming" not in result
