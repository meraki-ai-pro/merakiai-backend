"""Whose voice a student hears, and what must never leak.

The rule the whole feature rests on: **the voice belongs to the COURSE.** A
concept video is rendered once for a whole cohort and the lesson board is the
same lesson for everyone on that course, so neither can follow a per-student
preference. Everything below is a way of holding that rule, or of holding the
fallbacks that keep a lesson audible when it cannot be met.
"""

import ast
import inspect
from pathlib import Path

import pytest

from app.media import voices

BACKEND = Path(__file__).resolve().parents[2]


class FakeQuery:
    def __init__(self, rows, raises=None):
        self._rows = rows
        self._raises = raises

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        if self._raises:
            raise self._raises
        return type("R", (), {"data": self._rows})()


class FakeSupabase:
    """Answers `courses` and `avatar_voice_bundles` from canned rows."""

    def __init__(self, courses=None, bundles=None, raises=None):
        self.courses = courses or []
        self.bundles = bundles or []
        self.raises = raises

    def table(self, name):
        if name == "courses":
            return FakeQuery(self.courses, self.raises)
        return FakeQuery(self.bundles)


def course_row(provider_voice_id="lect-voice", status="ready", deleted=None, embedded=True):
    voice = {
        "provider_voice_id": provider_voice_id,
        "status": status,
        "deleted_at": deleted,
    }
    return {
        "lecturer_voice_id": "v1",
        # PostgREST returns an embedded row as an object on some relationship
        # shapes and a single-element list on others. Both are normal.
        "lecturer_voices": voice if embedded else [voice],
    }


@pytest.fixture(autouse=True)
def no_default_voice(monkeypatch):
    """Most cases assert the lecturer voice wins, so the house voice must not
    silently satisfy them."""
    monkeypatch.setattr(voices, "DEFAULT_NARRATION_VOICE_ID", "")


class TestResolutionOrder:
    def test_the_lecturers_voice_wins(self, monkeypatch):
        monkeypatch.setattr(
            voices, "get_supabase",
            lambda: FakeSupabase(courses=[course_row()], bundles=[{"voice_id": "bundle"}]),
        )
        assert voices.voice_for_course("calculus-101") == "lect-voice"

    def test_the_embedded_row_may_arrive_as_a_list(self, monkeypatch):
        monkeypatch.setattr(
            voices, "get_supabase",
            lambda: FakeSupabase(courses=[course_row(embedded=False)]),
        )
        assert voices.voice_for_course("calculus-101") == "lect-voice"

    def test_the_house_voice_is_used_when_the_lecturer_recorded_none(self, monkeypatch):
        monkeypatch.setattr(voices, "DEFAULT_NARRATION_VOICE_ID", "house")
        monkeypatch.setattr(
            voices, "get_supabase",
            lambda: FakeSupabase(courses=[{"lecturer_voice_id": None, "lecturer_voices": None}]),
        )
        assert voices.voice_for_course("calculus-101") == "house"

    def test_an_active_bundle_is_the_last_resort(self, monkeypatch):
        """A deployment that configured nothing still speaks."""
        monkeypatch.setattr(
            voices, "get_supabase",
            lambda: FakeSupabase(courses=[], bundles=[{"voice_id": "bundle"}]),
        )
        assert voices.voice_for_course("calculus-101") == "bundle"

    def test_nothing_configured_returns_none_rather_than_raising(self, monkeypatch):
        """The caller then skips audio. A lesson without narration beats a
        lesson that fails to load."""
        monkeypatch.setattr(voices, "get_supabase", lambda: FakeSupabase())
        assert voices.voice_for_course("calculus-101") is None

    def test_no_course_still_resolves_the_house_voice(self, monkeypatch):
        monkeypatch.setattr(voices, "DEFAULT_NARRATION_VOICE_ID", "house")
        monkeypatch.setattr(voices, "get_supabase", lambda: FakeSupabase())
        assert voices.voice_for_course(None) == "house"


class TestAVoiceMustBeUsableToBeUsed:
    @pytest.mark.parametrize(
        "row",
        [
            course_row(status="pending"),
            course_row(status="failed"),
            course_row(deleted="2026-08-19T00:00:00Z"),
            course_row(provider_voice_id=None),
        ],
        ids=["pending", "failed", "deleted", "no-provider-id"],
    )
    def test_an_unusable_voice_falls_through(self, monkeypatch, row):
        """Half-created and retired voices must not be spoken with — the
        provider would reject them and the lesson would go silent."""
        monkeypatch.setattr(voices, "DEFAULT_NARRATION_VOICE_ID", "house")
        monkeypatch.setattr(voices, "get_supabase", lambda: FakeSupabase(courses=[row]))
        assert voices.voice_for_course("calculus-101") == "house"

    def test_a_database_error_falls_back_instead_of_raising(self, monkeypatch):
        """sql/014 may not be applied. Narration must degrade, not break."""
        monkeypatch.setattr(voices, "DEFAULT_NARRATION_VOICE_ID", "house")
        monkeypatch.setattr(
            voices, "get_supabase",
            lambda: FakeSupabase(raises=Exception('column "lecturer_voice_id" does not exist')),
        )
        assert voices.voice_for_course("calculus-101") == "house"


class TestTheProviderVoiceIdNeverLeaves:
    """A provider voice id is a voice anyone holding our API key can speak as.

    Students never see one, and neither does the lecturer's browser: the server
    resolves the course's voice and returns AUDIO.
    """

    def test_the_lecturer_api_does_not_return_it(self):
        from app.api.v1.lecturer.voices import _VOICE_COLUMNS

        assert "provider_voice_id" not in _VOICE_COLUMNS

    def test_the_frontend_type_does_not_carry_it(self):
        types = BACKEND.parent / "merakiai-frontend" / "src" / "types" / "lecturer.ts"
        if not types.exists():  # pragma: no cover — backend-only checkout
            pytest.skip("frontend not present")
        source = types.read_text(encoding="utf-8")
        block = source[source.index("export interface LecturerVoice"):]
        block = block[: block.index("}")]
        assert "provider_voice_id" not in block

    def test_there_is_no_student_policy_on_the_voice_table(self):
        migration = (BACKEND / "sql" / "014_lecturer_voices.sql").read_text(encoding="utf-8")
        assert "is_enrolled" not in migration


class TestBoardNarration:
    def test_the_cache_key_includes_the_voice(self):
        """Keying on text alone would serve one course's audio — one
        lecturer's actual voice — to a different course."""
        from app.api.v1.narration import _cache_path

        same_text = "The chain rule says d y by d x equals d y by d u times d u by d x."
        assert _cache_path("voice-a", same_text) != _cache_path("voice-b", same_text)

    def test_the_same_slide_in_the_same_voice_is_one_file(self):
        from app.api.v1.narration import _cache_path

        text = "Water is split, releasing oxygen."
        assert _cache_path("v", text) == _cache_path("v", text)

    def test_enrolment_is_required(self):
        """This endpoint spends money per call, so a token alone must not be
        enough to make it spend."""
        from app.api.v1 import narration

        source = inspect.getsource(narration.narrate_board_slide)
        assert "require_enrolment" in source

    def test_the_text_is_capped(self):
        from app.api.v1.narration import MAX_TEXT_CHARS

        assert 0 < MAX_TEXT_CHARS <= 5000


class TestOwnershipIsCheckedOnBothSides:
    def test_attaching_a_voice_checks_the_course_and_the_voice(self):
        """Checking only the course would let a lecturer point their course at
        somebody else's recorded voice."""
        from app.api.v1.lecturer import voices as voice_api

        source = inspect.getsource(voice_api.set_course_voice)
        assert "assert_course_owner" in source
        assert "_owned_voice" in source

    def test_every_voice_route_is_lecturer_guarded(self):
        module = BACKEND / "app" / "api" / "v1" / "lecturer" / "voices.py"
        tree = ast.parse(module.read_text(encoding="utf-8"))

        unguarded = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorated = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in {"get", "post", "patch", "delete", "put"}
                for d in node.decorator_list
            )
            if decorated and "lecturer_guard" not in ast.dump(node):
                unguarded.append(node.name)

        assert not unguarded, f"unguarded voice routes: {unguarded}"

    def test_a_voice_belonging_to_someone_else_is_a_404(self):
        """403 would confirm the id exists — the same reasoning as courses."""
        from app.api.v1.lecturer import voices as voice_api

        source = inspect.getsource(voice_api._owned_voice)
        assert "404" in source
        assert 'eq("owner_id"' in source


class TestCloneFailuresBlameTheRightPerson:
    """A lecturer must never be sent to re-record a sample that was fine.

    The first real cloning failure was an API key without the
    `create_instant_voice_clone` permission. A single generic "check your
    recording" message would have had a lecturer recording the same passage
    over and over against a wall only an administrator can move.
    """

    def _error(self, status, body=""):
        exc = RuntimeError(body or "boom")
        exc.status_code = status
        exc.body = body
        return exc

    def test_a_permissions_failure_says_so_and_absolves_the_recording(self):
        message = voices._clone_failure_message(
            self._error(401, "missing_permissions create_instant_voice_clone")
        )
        assert "permission" in message.lower()
        assert "re-recording will not help" in message.lower()

    def test_a_quota_failure_names_the_quota(self):
        message = voices._clone_failure_message(self._error(429, "quota exceeded"))
        assert "slot" in message.lower() or "limit" in message.lower()

    def test_a_rejected_recording_asks_for_a_better_one(self):
        message = voices._clone_failure_message(self._error(422, "audio too short"))
        assert "30 seconds" in message

    def test_an_unrecognised_failure_does_not_only_blame_the_recording(self):
        message = voices._clone_failure_message(RuntimeError("something else"))
        assert "configuration" in message.lower()


class TestTheNarrationUrlIsActuallyFetchable:
    """A URL that only fails when something downloads it.

    The first version omitted the bucket segment. Every URL was wrong in the
    same way, so cache-hit and different-voice assertions all still passed —
    they compared broken URLs to each other. Only fetching one caught it.
    """

    def test_the_url_contains_the_bucket_and_the_path(self, monkeypatch):
        from app.api.v1 import narration

        monkeypatch.setattr(
            narration, "PUBLIC_BASE", "https://x.supabase.co/storage/v1/object/public"
        )
        monkeypatch.setattr(narration, "BUCKET", "audio-files")

        url = narration._public_url("board-narration/abc.mp3")
        assert url == (
            "https://x.supabase.co/storage/v1/object/public/"
            "audio-files/board-narration/abc.mp3"
        )

    def test_it_matches_how_the_rest_of_the_app_builds_audio_urls(self, monkeypatch):
        """storage_service already had the convention; diverging from it is
        what produced the broken URL."""
        from app.api.v1 import narration

        base = "https://x.supabase.co/storage/v1/object/public"
        monkeypatch.setattr(narration, "PUBLIC_BASE", base)
        monkeypatch.setattr(narration, "BUCKET", "audio-files")

        path = "board-narration/abc.mp3"
        assert narration._public_url(path) == f"{base}/audio-files/{path}"

    @pytest.mark.parametrize("missing", ["PUBLIC_BASE", "BUCKET"])
    def test_an_unconfigured_bucket_returns_none_rather_than_a_broken_url(
        self, monkeypatch, missing
    ):
        """None makes the endpoint return the audio inline. A half-built URL
        would be handed to a student and fail in their browser."""
        from app.api.v1 import narration

        monkeypatch.setattr(narration, "PUBLIC_BASE", "https://x/y")
        monkeypatch.setattr(narration, "BUCKET", "audio-files")
        monkeypatch.setattr(narration, missing, "")

        assert narration._public_url("board-narration/abc.mp3") is None


class TestTheApiKeyIsAKeyNotAnId:
    """The ElevenLabs dashboard shows a key's ID next to it and the secret only
    once, at creation. Copying the visible one is the natural mistake, and it
    surfaces as a 400 from inside a TTS call hours later.
    """

    def _client(self, monkeypatch, key):
        from app.media import tts_service

        monkeypatch.setattr(tts_service, "_eleven", None)
        monkeypatch.setattr(tts_service, "_eleven_key", None)
        monkeypatch.setattr("app.core.media_config.get_key", lambda _name: key)
        return tts_service

    def test_a_key_id_is_rejected_with_an_actionable_message(self, monkeypatch):
        tts_service = self._client(monkeypatch, "f92" + "a" * 61)
        with pytest.raises(RuntimeError, match="looks like an API key ID"):
            tts_service._get_eleven()

    def test_the_message_says_where_to_get_the_real_one(self, monkeypatch):
        tts_service = self._client(monkeypatch, "0" * 64)
        with pytest.raises(RuntimeError) as exc:
            tts_service._get_eleven()
        assert "sk_" in str(exc.value)
        assert "rotate" in str(exc.value).lower()

    def test_a_missing_key_still_says_it_is_missing(self, monkeypatch):
        tts_service = self._client(monkeypatch, "")
        with pytest.raises(RuntimeError, match="is not set"):
            tts_service._get_eleven()

    def test_a_real_looking_key_is_accepted(self, monkeypatch):
        """Matched narrowly, so a future change to the key format cannot lock
        out a valid key."""
        tts_service = self._client(monkeypatch, "sk_" + "0" * 48)
        assert tts_service._get_eleven() is not None

    def test_a_configuration_error_reaches_the_lecturer_intact(self):
        """Replacing a precise pre-flight message with a guess loses the one
        piece of information that would fix it."""
        exc = RuntimeError(
            "ELEVENLABS_API_KEY looks like an API key ID, not an API key."
        )
        message = voices._clone_failure_message(exc)
        assert "administrator" in message.lower()
        assert "API key ID" in message
