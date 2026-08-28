"""Whose voice speaks, and how a lecturer's gets cloned.

Two jobs that belong together because they share one rule: **the voice a
student hears is a property of the COURSE, not of the listener.** A concept
video is rendered once and replayed by a whole cohort, and the lesson board is
the same lesson for everyone on that course, so neither can follow a personal
preference the way the D-ID avatar does.

Resolution order, and each step earns its place:

  1. the voice the lecturer recorded and attached to this course — the point of
     the feature;
  2. a configured house voice (``DEFAULT_NARRATION_VOICE_ID``) — "a very good,
     educative and clear voice", chosen once rather than per course;
  3. the first active avatar bundle, so a deployment that has configured
     nothing still speaks rather than falling silent.

A missing voice is never an error here. Silence in a lesson is a worse failure
than an unfamiliar voice.
"""

from __future__ import annotations

import logging
import os

from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)

# The house narrator, used by every course whose lecturer has not recorded one.
# Set this to a clear, well-paced ElevenLabs voice; the fallback below is only
# a safety net and follows whatever the avatar bundles happen to hold.
DEFAULT_NARRATION_VOICE_ID = os.getenv("DEFAULT_NARRATION_VOICE_ID", "").strip()

# Instant voice cloning needs enough audio to model a voice and not so much
# that an upload stalls. ElevenLabs asks for at least ~30s of clean speech;
# below about 20 the clone is recognisably poor, which is worth refusing rather
# than letting a lecturer conclude the feature is broken.
MIN_SAMPLE_SECONDS = 20
MAX_SAMPLE_SECONDS = 300
MAX_SAMPLE_BYTES = 25 * 1024 * 1024

ALLOWED_SAMPLE_TYPES = {
    "audio/webm", "audio/ogg", "audio/mpeg", "audio/mp3",
    "audio/wav", "audio/x-wav", "audio/mp4", "audio/m4a", "audio/x-m4a",
}


class VoiceCloneError(RuntimeError):
    """Cloning failed for a reason worth showing the lecturer."""


# ── Resolution ──────────────────────────────────────────────────────────────

def _fallback_voice_id() -> str | None:
    """Last resort: whatever active avatar bundle exists."""
    try:
        rows = (
            get_supabase()
            .table("avatar_voice_bundles")
            .select("voice_id")
            .eq("is_active", True)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 — narration must not fail over this
        logger.warning("Could not read a fallback voice: %s", exc)
        return None
    return rows[0]["voice_id"] if rows else None


def voice_for_course(course_id: str | None) -> str | None:
    """The ElevenLabs voice id this course should be spoken in.

    Never raises. Returns None only when nothing at all is configured, and the
    caller then skips audio rather than failing the lesson.
    """
    if course_id:
        try:
            rows = (
                get_supabase()
                .table("courses")
                .select("lecturer_voice_id, lecturer_voices(provider_voice_id, status, deleted_at)")
                .eq("id", course_id)
                .limit(1)
                .execute()
                .data
                or []
            )
        except Exception as exc:  # noqa: BLE001 — sql/014 may not be applied
            logger.warning("Could not read the voice for course %s: %s", course_id, exc)
            rows = []

        if rows:
            voice = rows[0].get("lecturer_voices")
            # PostgREST returns an embedded row as an object, or a list on some
            # relationship shapes. Both are normal; neither is worth a branch
            # at every call site.
            if isinstance(voice, list):
                voice = voice[0] if voice else None
            if (
                voice
                and voice.get("status") == "ready"
                and not voice.get("deleted_at")
                and voice.get("provider_voice_id")
            ):
                return voice["provider_voice_id"]

    if DEFAULT_NARRATION_VOICE_ID:
        return DEFAULT_NARRATION_VOICE_ID

    return _fallback_voice_id()


# ── Cloning ─────────────────────────────────────────────────────────────────

def _clone_failure_message(exc: Exception) -> str:
    """Turn a provider error into something the right person can act on.

    This matters more than it looks. A single "check your recording" message
    sends a lecturer into a loop of re-recording a sample that was never the
    problem — the first real failure here was an API key without the
    `create_instant_voice_clone` permission, which no amount of re-recording
    fixes and which only an administrator can.
    """
    status = getattr(exc, "status_code", None)
    body = str(getattr(exc, "body", "") or exc).lower()

    # Our own pre-flight checks already say exactly what is wrong and how to
    # fix it — passing them through beats replacing them with a guess. Prefixed
    # so the lecturer knows this is not something they can fix by re-recording,
    # and knows to forward it.
    if isinstance(exc, RuntimeError) and "elevenlabs_api_key" in body:
        return f"Voice cloning is not configured. Ask an administrator: {exc}"

    if status in (401, 403) or "missing_permissions" in body or "unauthorized" in body:
        return (
            "Voice cloning is not enabled on this ElevenLabs account. An "
            "administrator needs to grant the API key the "
            "'create_instant_voice_clone' permission (voice cloning also "
            "requires a paid ElevenLabs plan). Your recording was fine — "
            "re-recording will not help."
        )
    if status == 429 or "quota" in body or "limit" in body:
        return (
            "This ElevenLabs account has no voice slots or characters left. "
            "An administrator needs to free a slot or raise the plan limit."
        )
    if status == 422 or "too short" in body or "invalid" in body:
        return (
            "The provider rejected the recording. Record at least 30 seconds "
            "of clear, continuous speech somewhere quiet, then try again."
        )
    return (
        "The voice could not be created. Check the recording is clear speech "
        "of at least 30 seconds, then try again — if it keeps failing, this is "
        "an ElevenLabs configuration problem rather than your recording."
    )


def create_cloned_voice(
    *, name: str, samples: list[tuple[str, bytes]], description: str | None = None
) -> str:
    """Clone a voice from recorded samples. Returns the provider voice id.

    The audio is sent and then forgotten: we store the resulting voice id, not
    the recording, so this never becomes a store of biometric data.
    """
    from app.media.tts_service import _get_eleven

    if not samples:
        raise VoiceCloneError("No audio was uploaded.")

    try:
        created = _get_eleven().voices.ivc.create(
            name=name,
            files=[(filename, content) for filename, content in samples],
            # A lecturer records at a desk, not in a booth. This is the
            # difference between a clone that sounds like them and one that
            # sounds like their room.
            remove_background_noise=True,
            description=description,
        )
    except Exception as exc:  # noqa: BLE001 — provider errors are many and untyped
        logger.error("Voice cloning failed for %r: %s", name, exc)
        raise VoiceCloneError(_clone_failure_message(exc)) from exc

    voice_id = getattr(created, "voice_id", None)
    if not voice_id:
        raise VoiceCloneError("The provider did not return a voice id.")
    return voice_id


def delete_cloned_voice(provider_voice_id: str) -> bool:
    """Remove a cloned voice from the provider. True if it is gone.

    Best-effort by design: the local row is what governs whether the voice is
    USED, so a provider hiccup must not block a lecturer from retiring their
    voice in the product.
    """
    if not provider_voice_id:
        return True
    from app.media.tts_service import _get_eleven

    try:
        _get_eleven().voices.delete(provider_voice_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not delete voice %s at the provider: %s", provider_voice_id, exc)
        return False
