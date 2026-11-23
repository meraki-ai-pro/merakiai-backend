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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import auth_guard
from app.core.enrolment import require_enrolment
from app.core.rate_limit import rate_limit
from app.media.storage_service import BUCKET, PUBLIC_BASE
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
    # Already converted from LaTeX to spoken words by the client (lib/speech.ts)
    # — the same conversion the browser voice used, so both paths say "d y by
    # d x" rather than reading backslashes aloud.
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_CHARS)
    # The slide's raw markdown (title + body) the text was made from. The
    # server checks it against what the tutor actually said to this student,
    # so the endpoint cannot be used to put arbitrary sentences in a
    # lecturer's cloned voice.
    source: str = Field(..., min_length=1, max_length=8000)


# Words the client's maths-to-speech conversion introduces. They never appear
# in the markdown, so they must not count against the text.
_SPOKEN_MATH_WORDS = frozenset(
    "over squared cubed equals plus minus times divided power sub root square cube "
    "integral integrals sum product limit approaches infinity choose less greater "
    "than equal not approximately partial pi theta alpha beta mu sigma lambda delta "
    "real numbers natural integers and so on see table board figure plus or is the of "
    "to from as in dollars".split()
)
_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_MIN_TEXT_COVERAGE = 0.8
# Recent tutor turns searched for the slide. A lesson being listened to is
# the one just given; this is generous without being an unbounded scan.
_RECENT_TURNS = 60


def _norm(s: str) -> str:
    return " ".join(s.split())


def source_is_tutor_text(source: str, tutor_responses: list[str]) -> bool:
    """True when every line of ``source`` appears in one recent tutor answer."""
    lines = [_norm(l) for l in source.splitlines()]
    lines = [l for l in lines if l]
    if not lines:
        return False
    corpus = [_norm(r) for r in tutor_responses if r]
    return any(all(l in doc for l in lines) for doc in corpus)


def text_matches_source(text: str, source: str) -> bool:
    """True when the spoken text is made of the source's words.

    The client converts LaTeX to words, so the text is not a substring of the
    source; but its ordinary words must come from it. An arbitrary sentence
    paired with a genuine slide fails here.
    """
    src_words = {w.lower() for w in _WORD_RE.findall(source)} | _SPOKEN_MATH_WORDS
    words = [w.lower() for w in _WORD_RE.findall(text)]
    if not words:
        return True
    hits = sum(1 for w in words if w in src_words)
    return hits / len(words) >= _MIN_TEXT_COVERAGE


def _exam_question_texts(sb, user_id: str, course_id: str) -> list[str]:
    """Question text of exams this student may sit, formatted as the Listen
    button reads it (prompt, then "Option A: ..." lines). Lecturer-authored
    content, so it is safe to voice in the lecturer's voice."""
    from app.api.v1.assessments import visible_to

    papers = [
        a for a in (
            sb.table("assessments").select("*").eq("course_id", course_id)
            .eq("is_published", True).limit(50).execute().data or []
        )
        if visible_to(a, user_id)
    ]
    if not papers:
        return []
    rows = (
        sb.table("assessment_questions").select("prompt, options")
        .in_("assessment_id", [a["id"] for a in papers]).limit(500).execute().data or []
    )
    docs = []
    for q in rows:
        opts = q.get("options") or []
        lines = [q.get("prompt") or ""] + [
            f"Option {chr(65 + i)}: {o}" for i, o in enumerate(opts)
        ]
        docs.append("\n".join(lines))
    return docs


def _recent_tutor_responses(user_id: str, course_id: str) -> list[str]:
    from app.db.supabase import get_supabase

    sb = get_supabase()
    sessions = (
        sb.table("sessions").select("id").eq("user_id", user_id).eq("course_id", course_id)
        .order("created_at", desc=True).limit(20).execute().data or []
    )
    ids = [s["id"] for s in sessions]
    if not ids:
        return _exam_question_texts(sb, user_id, course_id)
    rows = (
        sb.table("conversations").select("tutor_response").in_("session_id", ids)
        .eq("user_id", user_id).order("created_at", desc=True).limit(_RECENT_TURNS)
        .execute().data or []
    )
    return [r.get("tutor_response") or "" for r in rows] + _exam_question_texts(
        sb, user_id, course_id
    )


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

    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Nothing to say.")

    if not text_matches_source(text, payload.source) or not source_is_tutor_text(
        payload.source, _recent_tutor_responses(user["id"], payload.course_id)
    ):
        raise HTTPException(
            status_code=403,
            detail="Only lesson content from your own tutor session can be narrated.",
        )

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
            return {"url": cached, "cached": True, "voice_id_hash": voice_id[:6]}
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

    return {"url": url, "cached": False}
