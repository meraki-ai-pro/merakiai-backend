"""Speaking a lesson-board slide aloud, in the course lecturer's voice.

The board used to be narrated by the browser's own speech synthesis. That was
free and instant, and it sounded like a satnav. The client asked for the
lecturer's own voice instead, which means hosted TTS — so this endpoint exists,
and with it two costs the browser did not have: money per slide and latency
before the first word.

Both are handled here rather than pushed onto the client:

  * **Cached by content.** The key is the voice plus the exact text, so the
    second student to open a lesson pays nothing and waits only for a download.
    A cohort of two hundred synthesises each slide once. Without this the same
    twelve slides would be re-synthesised two hundred times a night.

  * **Capped.** A slide is a paragraph; anything longer is not a slide and is
    refused rather than silently billed for.

The client keeps its browser-speech path as a fallback, so a failure here is a
worse-sounding lesson rather than a silent one.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import auth_guard
from app.core.enrolment import require_enrolment
from app.core.rate_limit import rate_limit
from app.media.storage_service import BUCKET, PUBLIC_BASE
from app.media.spoken import chunk_for_speech, spoken_answer
from app.media.voices import voice_for_course

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/narration", tags=["Narration"])

# A slide's worth of prose. Long enough for a dense paragraph of maths read
# aloud, short enough that one request cannot become a chapter.
MAX_TEXT_CHARS = 1200

# Where cached slide audio lives inside the existing audio bucket.
_CACHE_PREFIX = "board-narration"


class BoardNarrationRequest(BaseModel):
    course_id: str = Field(..., max_length=100)
    # The raw markdown being read (a slide, a tutor answer, an exam question).
    # The server verifies it against what the tutor actually said to this
    # student, derives the spoken text itself, and voices chunk ``chunk`` of it.
    # Nothing the client says about *how* it sounds is trusted.
    source: str = Field(..., min_length=1, max_length=8000)
    chunk: int = Field(0, ge=0, le=60)


_RECENT_TURNS = 60


def _norm(s: str) -> str:
    return " ".join(s.split())


# A run of words the student typed, appearing in what would be spoken, is an
# echo. The run is six words, or the whole input when it is shorter, down to
# three: below that every answer repeats its question ("what is a collector").
_ECHO_WINDOW = 6
_ECHO_MIN_WINDOW = 3


def _words(text: str) -> list[str]:
    # Unicode-aware, so non-Latin text is compared rather than dropped.
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())


def _windows(words: list[str], size: int) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def echoes_student(source: str, student_inputs: list[str]) -> bool:
    """True when the spoken form of ``source`` repeats a run of words the
    student typed ("repeat after me: ..."), which would otherwise pass as
    tutor content. Both sides go through the same spoken-text conversion, so
    markdown and maths notation cannot make them differ."""
    spoken = _words(spoken_answer(source))
    for typed in student_inputs:
        words = _words(spoken_answer(typed or ""))
        size = min(_ECHO_WINDOW, len(words))
        if size < _ECHO_MIN_WINDOW:
            continue
        if _windows(spoken, size) & _windows(words, size):
            return True
    return False


def source_is_tutor_text(source: str, tutor_responses: list[str]) -> bool:
    """True when the whole of ``source`` is one contiguous passage of an answer.

    Contiguity is the point. Matching lines (even in order) independently lets
    someone assemble a new sentence from words scattered through a long answer;
    a single unbroken passage can only be something the tutor said. A slide
    whose prose is interrupted by a plot or video block will not match and is
    read by the browser's voice instead.
    """
    needle = _norm(source)
    if not needle:
        return False
    return any(needle in _norm(r) for r in tutor_responses if r)


def _exam_question_texts(sb, user_id: str, course_id: str) -> list[str]:
    """Question text of papers this student may sit RIGHT NOW, formatted as the
    Listen button reads it. Closed-window papers are excluded, so the endpoint
    cannot be used to probe questions that are not yet open."""
    from fastapi import HTTPException as _HTTP

    from app.api.v1.assessments import _check_window, visible_to

    papers = []
    for a in (
        sb.table("assessments").select("*").eq("course_id", course_id)
        .eq("is_published", True).limit(50).execute().data or []
    ):
        if not visible_to(a, user_id):
            continue
        try:
            _check_window(a)
        except _HTTP:
            continue
        papers.append(a)
    if not papers:
        return []
    rows = (
        sb.table("assessment_questions").select("prompt, options")
        .in_("assessment_id", [a["id"] for a in papers]).limit(500).execute().data or []
    )
    docs = []
    for q in rows:
        lines = [q.get("prompt") or ""] + [
            f"Option {chr(65 + i)}: {o}" for i, o in enumerate(q.get("options") or [])
        ]
        docs.append("\n".join(lines))
    return docs


def _recent_turns(user_id: str, course_id: str) -> tuple[list[str], list[str]]:
    """(tutor-side texts, what the student typed) for this student and course."""
    from app.db.supabase import get_supabase

    sb = get_supabase()
    sessions = (
        sb.table("sessions").select("id").eq("user_id", user_id).eq("course_id", course_id)
        .order("created_at", desc=True).limit(20).execute().data or []
    )
    ids = [s["id"] for s in sessions]
    rows = []
    if ids:
        rows = (
            sb.table("conversations").select("tutor_response, user_input").in_("session_id", ids)
            .eq("user_id", user_id).order("created_at", desc=True).limit(_RECENT_TURNS)
            .execute().data or []
        )
    tutor = [r.get("tutor_response") or "" for r in rows] + _exam_question_texts(
        sb, user_id, course_id
    )
    return tutor, [r.get("user_input") or "" for r in rows]


def _cache_path(voice_id: str, text: str) -> str:
    """Content-addressed, so identical slides share one file.

    The voice is part of the key: the same sentence in a different lecturer's
    voice is a different recording, and keying on text alone would serve one
    course's audio to another.
    """
    digest = hashlib.sha256(f"{voice_id}\n{text}".encode("utf-8")).hexdigest()
    return f"{_CACHE_PREFIX}/{digest}.mp3"


def _public_url(path: str) -> str | None:
    """Public URL for an object in the audio bucket.

    The BUCKET segment is not optional. SUPABASE_STORAGE_PUBLIC_BASE ends at
    `/object/public`, so omitting it produces a URL whose first path segment is
    the cache prefix — which looks entirely plausible, compares equal to other
    equally-broken URLs, and 400s only when something actually downloads it.
    """
    if not PUBLIC_BASE or not BUCKET:
        return None
    return f"{PUBLIC_BASE.rstrip('/')}/{BUCKET}/{path.lstrip('/')}"


@router.post("/board")
def narrate_board_slide(
    payload: BoardNarrationRequest,
    user=Depends(auth_guard),
    _rl=Depends(rate_limit(max_calls=60, window_seconds=60)),
):
    """Audio for one lesson-board slide, in this course's voice.

    Enrolment is enforced: this endpoint spends money per call, so it must not
    be reachable by anyone holding a token who is not actually on the course.
    """
    require_enrolment(user, payload.course_id)

    # One refusal for every reason, so the response says nothing about which
    # check failed or whether a guessed passage exists.
    tutor_texts, typed = _recent_turns(user["id"], payload.course_id)
    if not source_is_tutor_text(payload.source, tutor_texts) or echoes_student(
        payload.source, typed
    ):
        raise HTTPException(
            status_code=403,
            detail="Only lesson content from your own tutor session can be narrated.",
        )

    chunks = chunk_for_speech(spoken_answer(payload.source), MAX_TEXT_CHARS - 50)
    if not chunks or payload.chunk >= len(chunks):
        raise HTTPException(status_code=400, detail="Nothing to say.")
    text = chunks[payload.chunk]
    total = len(chunks)

    voice_id = voice_for_course(payload.course_id)
    if not voice_id:
        # No lecturer voice, no house voice, no bundle. The client falls back
        # to browser speech, so this is a 404-shaped fact rather than an error.
        raise HTTPException(
            status_code=503,
            detail="No narration voice is configured for this course.",
        )

    path = _cache_path(voice_id, text)
    cached = _public_url(path)

    from app.db.supabase import get_supabase

    storage = get_supabase().storage.from_(BUCKET)

    # Cache probe. A miss is the normal first case and must not look like a
    # failure, so any error here just falls through to synthesis.
    try:
        existing = storage.list(
            _CACHE_PREFIX, {"search": path.rsplit("/", 1)[-1], "limit": 1}
        )
        if existing and cached:
            return {"url": cached, "cached": True, "total": total, "voice_id_hash": voice_id[:6]}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Narration cache probe failed for %s: %s", path, exc)

    from app.media.tts_service import tts_to_mp3_bytes

    try:
        audio = tts_to_mp3_bytes(text, voice_id=voice_id)
    except Exception as exc:  # noqa: BLE001 — provider outage, quota, bad key
        logger.warning("Board narration failed for course %s: %s", payload.course_id, exc)
        raise HTTPException(
            status_code=502, detail="The narration voice is unavailable right now."
        ) from exc

    try:
        storage.upload(
            path=path,
            file=audio,
            file_options={"content-type": "audio/mpeg", "upsert": "true"},
        )
    except Exception as exc:  # noqa: BLE001
        # The audio is good; only the cache write failed. Returning it inline
        # beats failing the slide, at the cost of re-synthesising next time.
        logger.warning("Could not cache board narration: %s", exc)
        from fastapi.responses import Response

        return Response(content=audio, media_type="audio/mpeg")

    url = _public_url(path)
    if not url:
        from fastapi.responses import Response

        return Response(content=audio, media_type="audio/mpeg")

    return {"url": url, "cached": False, "total": total}
