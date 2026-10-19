"""Lecture videos as course material: transcribe, then index like a document.

The transcript is cut into timed sections whose headings carry the time span
("Video 12:00 – 14:00"). The heading becomes the chunk's section title, so a
citation already says where in the video the passage is, and the student-side
"Watch from" link reads its start time from that same heading. No new metadata
field travels through the retriever for it.

Speech-to-text is ElevenLabs (the key the narration pipeline already holds).
It fetches the media itself — a signed storage URL for an upload, the page URL
for a link (YouTube and other hosts are supported) — so a long video never
passes through the API or the worker.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, Iterable, List

from app.ai.ingestion.math_parser import HEADING, PARAGRAPH

VIDEO_CONTENT_TYPES = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
}

# A section is what a citation points at: short enough that "from 12:00" lands
# near the passage, long enough to hold a complete explanation.
SECTION_SECONDS = 120
# Paragraphs inside a section, so the chunker has natural places to split.
PARAGRAPH_SECONDS = 30

HEADING_PREFIX = "Video "
_SENTENCE_END = re.compile(r"[.?!]$")


def clock(seconds: float) -> str:
    """12:05, or 1:02:05 past the hour."""
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def transcript_blocks(words: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Word timings -> heading + paragraph blocks the chunker understands.

    ``words`` are ElevenLabs word objects ({"text", "start", "end", "type"});
    spacing and audio-event entries are skipped.
    """
    spoken = [w for w in words if w.get("type", "word") == "word" and str(w.get("text", "")).strip()]
    blocks: List[Dict[str, Any]] = []
    i = 0
    while i < len(spoken):
        start = float(spoken[i].get("start") or 0)
        section_end = start + SECTION_SECONDS
        section: List[Dict[str, Any]] = []
        while i < len(spoken) and float(spoken[i].get("start") or 0) < section_end:
            section.append(spoken[i])
            i += 1
        end = float(section[-1].get("end") or section[-1].get("start") or start)
        title = f"{HEADING_PREFIX}{clock(start)} – {clock(end)}"
        blocks.append({"type": HEADING, "text": title, "heading_path": []})
        blocks.extend(
            {"type": PARAGRAPH, "text": text, "heading_path": [title]}
            for text in _paragraphs(section)
        )
    return blocks


def _paragraphs(words: List[Dict[str, Any]]) -> List[str]:
    """Break at the first sentence end after PARAGRAPH_SECONDS of speech."""
    out, current, began = [], [], None
    for w in words:
        text = str(w["text"]).strip()
        began = float(w.get("start") or 0) if began is None else began
        current.append(text)
        if float(w.get("end") or 0) - began >= PARAGRAPH_SECONDS and _SENTENCE_END.search(text):
            out.append(" ".join(current))
            current, began = [], None
    if current:
        out.append(" ".join(current))
    return out


async def transcribe(*, file_url: str | None = None, link: str | None = None) -> List[Dict[str, Any]]:
    """Word-level transcript of a video, fetched by ElevenLabs from the URL."""
    from app.media.tts_service import _get_eleven

    source_url = file_url or link
    if not source_url or bool(file_url) == bool(link):
        raise ValueError("Provide exactly one video URL to transcribe.")

    # ``cloud_storage_url`` is deprecated in the current ElevenLabs API.
    # ``source_url`` accepts both signed object-storage URLs and hosted pages
    # such as YouTube, so one code path serves uploads and links.
    result = await asyncio.to_thread(
        _get_eleven().speech_to_text.convert,
        model_id=os.getenv("ELEVENLABS_STT_MODEL_ID", "scribe_v2"),
        timestamps_granularity="word",
        tag_audio_events=False,
        source_url=source_url,
    )
    words = [w.model_dump() if hasattr(w, "model_dump") else dict(w) for w in (result.words or [])]
    if not words:
        raise RuntimeError("The video transcription returned no spoken words.")
    return words


def watch_url(media_url: str, start_seconds: int) -> str:
    """The video, opened at a moment.

    YouTube reads ``t=`` in the query; an MP4 (a signed storage URL included)
    honours the W3C media fragment ``#t=``, which survives signing because the
    fragment never reaches the server.
    """
    start = max(0, int(start_seconds))
    if re.match(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", media_url):
        return f"{media_url}{'&' if '?' in media_url else '?'}t={start}s"
    return f"{media_url}#t={start}"
