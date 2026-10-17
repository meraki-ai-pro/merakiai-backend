"""Lecture videos: transcript -> timed sections -> the chunker, and watch links."""

from app.ai.ingestion.math_parser import HEADING, PARAGRAPH
from app.ai.ingestion.structured_chunker import build_chunks
from app.ai.ingestion.video import clock, transcript_blocks, watch_url


def _words(n: int, step: float = 1.0):
    out = []
    for i in range(n):
        text = f"word{i}." if i % 12 == 11 else f"word{i}"
        out.append({"text": text, "start": i * step, "end": i * step + 0.8, "type": "word"})
        out.append({"text": " ", "start": i * step + 0.8, "end": i * step + 1, "type": "spacing"})
    return out


def test_clock():
    assert clock(0) == "0:00"
    assert clock(725) == "12:05"
    assert clock(3725) == "1:02:05"


def test_sections_are_two_minutes_and_titled_with_their_span():
    blocks = transcript_blocks(_words(300))  # five minutes of speech
    headings = [b["text"] for b in blocks if b["type"] == HEADING]
    assert headings == ["Video 0:00 – 1:59", "Video 2:00 – 3:59", "Video 4:00 – 4:59"]
    paragraphs = [b for b in blocks if b["type"] == PARAGRAPH]
    assert len(paragraphs) > len(headings)  # sections are split into paragraphs
    assert sum(len(b["text"].split()) for b in paragraphs) == 300  # every word kept once


def test_spacing_and_audio_events_are_dropped():
    words = [{"text": "(laughs)", "start": 0, "end": 1, "type": "audio_event"}, *_words(3)]
    text = " ".join(b["text"] for b in transcript_blocks(words) if b["type"] == PARAGRAPH)
    assert "laughs" not in text and text.startswith("word0")


def test_the_chunker_carries_the_time_span_as_the_section_title():
    chunks = build_chunks(transcript_blocks(_words(300)), document_id="d", source_filename="lecture.mp4", mode="learn")
    assert chunks and all(c["section_title"].startswith("Video ") for c in chunks)


def test_watch_links():
    assert watch_url("https://www.youtube.com/watch?v=abc", 125) == "https://www.youtube.com/watch?v=abc&t=125s"
    assert watch_url("https://youtu.be/abc", 5) == "https://youtu.be/abc?t=5s"
    assert watch_url("https://x.supabase.co/storage/v1/object/sign/v.mp4?token=t", 60).endswith("token=t#t=60")
