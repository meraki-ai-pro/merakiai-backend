"""A lecturer's recorded voice, and which of their courses it speaks for.

The flow is deliberately two steps rather than one. Recording a voice and
putting it in front of students are different decisions: a lecturer records,
listens to the preview, and only then attaches it to a course. Cloning straight
onto a live course would put an unheard voice in front of a cohort.

Ownership is checked on BOTH sides of the attach: the caller must own the voice
and own the course. Checking only the course would let a lecturer point their
course at somebody else's voice.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.core import audit
from app.core.auth import assert_course_owner, lecturer_guard
from app.db.supabase import get_supabase
from app.media.voices import (
    ALLOWED_SAMPLE_TYPES,
    MAX_SAMPLE_BYTES,
    MIN_SAMPLE_SECONDS,
    VoiceCloneError,
    create_cloned_voice,
    delete_cloned_voice,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voices", tags=["Lecturer – Voices"])

# What the preview reads back. Deliberately pedagogical rather than "testing,
# one two three": a lecturer judging their own clone needs to hear it say the
# kind of sentence it will actually say.
PREVIEW_TEXT = (
    "Hello. This is the voice your students will hear when I explain a concept "
    "on the lesson board, or narrate a short video for this course."
)

_VOICE_COLUMNS = "id, name, provider, status, error, sample_seconds, created_at"


class VoiceRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class CourseVoiceAssignment(BaseModel):
    # None detaches, falling the course back to the default narrator.
    voice_id: str | None = None


def _owned_voice(user: dict, voice_id: str) -> dict:
    rows = (
        get_supabase().table("lecturer_voices").select("*")
        .eq("id", voice_id).eq("owner_id", user["id"]).execute().data
    )
    # 404 rather than 403 for a voice owned by someone else — the same
    # reasoning as courses: a 403 confirms the id exists.
    if not rows or rows[0].get("deleted_at"):
        raise HTTPException(status_code=404, detail="Voice not found")
    return rows[0]


@router.get("")
def list_voices(user=Depends(lecturer_guard)):
    """Every voice this lecturer has recorded, with where each is in use."""
    sb = get_supabase()
    try:
        voices = (
            sb.table("lecturer_voices").select(_VOICE_COLUMNS)
            .eq("owner_id", user["id"]).is_("deleted_at", "null")
            .order("created_at", desc=True).execute().data or []
        )
    except Exception as exc:  # noqa: BLE001 — sql/014 may not be applied yet
        logger.warning("Voice list unavailable: %s", exc)
        return {"voices": [], "available": False}

    # Which courses each voice speaks for. One query, not one per voice.
    courses = (
        sb.table("courses").select("id, name, lecturer_voice_id")
        .eq("owner_id", user["id"]).execute().data or []
    )
    used_by: dict[str, list[dict]] = {}
    for course in courses:
        vid = course.get("lecturer_voice_id")
        if vid:
            used_by.setdefault(vid, []).append({"id": course["id"], "name": course["name"]})

    return {
        "voices": [{**v, "courses": used_by.get(v["id"], [])} for v in voices],
        "available": True,
    }


@router.post("")
async def create_voice(
    request: Request,
    sample: UploadFile,
    name: str = Form(...),
    seconds: float = Form(0),
    user=Depends(lecturer_guard),
):
    """Clone the lecturer's voice from one recording.

    The recording is sent to the provider and then dropped — only the resulting
    voice id is stored, so this never becomes a store of biometric data.
    """
    content_type = (sample.content_type or "").split(";")[0].strip()
    if content_type and content_type not in ALLOWED_SAMPLE_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported audio type {content_type!r}. Record in the browser, "
                   "or upload MP3, WAV, M4A, OGG or WebM.",
        )

    content = await sample.read(MAX_SAMPLE_BYTES + 1)
    if len(content) > MAX_SAMPLE_BYTES:
        raise HTTPException(status_code=413, detail="Recordings are limited to 25 MB.")
    if not content:
        raise HTTPException(status_code=400, detail="The recording is empty.")

    # Checked against what the CLIENT measured, because the duration of a
    # WebM/Opus blob is not readable without decoding it. A lecturer cannot
    # gain anything by lying — they would only get a poor clone of their own
    # voice — so this is guidance, not a security boundary.
    if seconds and seconds < MIN_SAMPLE_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Record at least {MIN_SAMPLE_SECONDS} seconds — a shorter "
                   "sample produces a clone that does not sound like you.",
        )

    sb = get_supabase()
    created = sb.table("lecturer_voices").insert({
        "owner_id": user["id"],
        "name": name.strip()[:120],
        "status": "pending",
        "sample_seconds": round(seconds, 1) if seconds else None,
    }).execute().data
    if not created:
        raise HTTPException(status_code=500, detail="Could not record the voice.")
    row = created[0]

    try:
        provider_voice_id = create_cloned_voice(
            name=f"{name.strip()[:60]} ({user['id'][:8]})",
            samples=[(sample.filename or "recording.webm", content)],
            description=f"Meraki lecturer voice for {user.get('email', user['id'])}",
        )
    except VoiceCloneError as exc:
        # The row is KEPT, marked failed. A lecturer who recorded for a minute
        # deserves to see what happened rather than an empty list.
        sb.table("lecturer_voices").update(
            {"status": "failed", "error": str(exc)[:500]}
        ).eq("id", row["id"]).execute()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    updated = sb.table("lecturer_voices").update({
        "status": "ready", "provider_voice_id": provider_voice_id, "error": None,
    }).eq("id", row["id"]).execute().data

    audit.record(
        actor=user, action="voice.create", resource_type="lecturer_voice",
        resource_id=row["id"], new_values={"name": name, "seconds": seconds},
        request=request,
    )

    voice = updated[0] if updated else row
    return {"status": "ok", "voice": {k: voice.get(k) for k in _VOICE_COLUMNS.split(", ")}}


@router.post("/{voice_id}/preview")
def preview_voice(voice_id: str, user=Depends(lecturer_guard)):
    """Speak a sample line so the lecturer can hear the clone before using it."""
    voice = _owned_voice(user, voice_id)
    if voice.get("status") != "ready" or not voice.get("provider_voice_id"):
        raise HTTPException(status_code=409, detail="This voice is not ready yet.")

    from fastapi.responses import Response

    from app.media.tts_service import tts_to_mp3_bytes

    try:
        audio = tts_to_mp3_bytes(PREVIEW_TEXT, voice_id=voice["provider_voice_id"])
    except Exception as exc:  # noqa: BLE001
        logger.error("Voice preview failed for %s: %s", voice_id, exc)
        raise HTTPException(status_code=502, detail="Could not play that voice.") from exc

    return Response(content=audio, media_type="audio/mpeg")


@router.patch("/{voice_id}")
def rename_voice(voice_id: str, payload: VoiceRename, user=Depends(lecturer_guard)):
    _owned_voice(user, voice_id)
    get_supabase().table("lecturer_voices").update(
        {"name": payload.name.strip()[:120]}
    ).eq("id", voice_id).execute()
    return {"status": "ok", "voice_id": voice_id, "name": payload.name}


@router.delete("/{voice_id}")
def delete_voice(voice_id: str, request: Request, user=Depends(lecturer_guard)):
    """Retire a voice. Courses using it fall back to the default narrator."""
    voice = _owned_voice(user, voice_id)
    sb = get_supabase()

    # Detach first. If the provider delete succeeded and this did not, a course
    # would point at a voice that no longer exists and every narration would
    # fail; this way the worst case is an orphaned voice at the provider.
    sb.table("courses").update({"lecturer_voice_id": None}).eq(
        "lecturer_voice_id", voice_id
    ).execute()

    removed = delete_cloned_voice(voice.get("provider_voice_id") or "")
    sb.table("lecturer_voices").update({"deleted_at": "now()"}).eq("id", voice_id).execute()

    audit.record(
        actor=user, action="voice.delete", resource_type="lecturer_voice",
        resource_id=voice_id, request=request,
    )
    return {"status": "ok", "voice_id": voice_id, "removed_at_provider": removed}


# ── Attaching a voice to a course ───────────────────────────────────────────
# Lives on the voices router rather than the courses one so the two ownership
# checks sit next to each other and cannot drift apart.

course_voice_router = APIRouter(
    prefix="/courses/{course_id}/voice", tags=["Lecturer – Voices"]
)


@course_voice_router.put("")
def set_course_voice(
    course_id: str,
    payload: CourseVoiceAssignment,
    request: Request,
    user=Depends(lecturer_guard),
):
    """Choose which recorded voice speaks for this course, or clear it."""
    assert_course_owner(user, course_id)

    if payload.voice_id:
        voice = _owned_voice(user, payload.voice_id)
        if voice.get("status") != "ready":
            raise HTTPException(
                status_code=409, detail="That voice is not ready to use yet."
            )

    get_supabase().table("courses").update(
        {"lecturer_voice_id": payload.voice_id}
    ).eq("id", course_id).execute()

    audit.record(
        actor=user, action="course.set_voice", resource_type="course",
        resource_id=course_id, course_id=course_id,
        new_values={"lecturer_voice_id": payload.voice_id}, request=request,
    )
    return {"status": "ok", "course_id": course_id, "voice_id": payload.voice_id}
