"""Spoken narration for a rendered concept video.

A silent animation is a worse teaching artefact than the Lesson Board it
replaces: the student has to read the screen and infer the reasoning at the
same time, with no voice telling them which line matters. So every render gets
a narration track that says out loud what the animation is showing.

**Why this is a second job and not part of the render.** The render worker
executes model-generated Python. render.Dockerfile therefore ships no
ElevenLabs client and docker-compose.render.yml passes no ElevenLabs key —
deliberately, so a payload that escapes the AST allowlist finds no credential
to steal and no HTTP client to steal it with. Adding TTS there would undo that
in one line. Instead the render worker publishes the silent mp4, marks the
asset ``narration_status='pending'``, and dispatches this work by name onto
``video_tasks``, where the ordinary media worker (which already has ffmpeg,
ElevenLabs and their keys) picks it up.

The consequence to keep in mind: an asset is briefly `ready` with no audio.
That is a real state, it is stored (`narration_status`), and the review queue
shows it rather than pretending the video is finished.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.db.supabase import get_supabase
from app.media.render.media_probe import binary, probe_duration as _probe_duration
from app.media.voices import voice_for_course

logger = logging.getLogger(__name__)

NARRATION_ENABLED = os.getenv("RENDER_NARRATION", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

# A global override, for a deployment that wants every video in one voice
# regardless of course. Normally EMPTY: the voice comes from the course, which
# is what makes a student hear their own lecturer.
#
# Distinct from the per-student avatar voice either way — a rendered asset is
# shared by the whole cohort and cached for ever, so it cannot follow one
# student's avatar choice.
NARRATION_VOICE_ID = os.getenv("RENDER_NARRATION_VOICE_ID", "").strip()

# Muxing is I/O-bound and quick, but ffmpeg on a pathological input can hang.
_FFMPEG_TIMEOUT = int(os.getenv("RENDER_FFMPEG_TIMEOUT", "180"))

# MEASURED, not assumed: 194 words of generated maths narration came back from
# ElevenLabs as 73.17s of audio on the chain-rule asset (2026-08-19), i.e. 2.65
# words per second. The first guess here was 2.3, which under-sized every budget
# by ~13% on top of whatever the model overshot by.
#
# Spoken mathematics is slower than prose — "d y by d x" is four words and
# barely half a second of meaning — so this is deliberately not a general
# speech-rate figure.
_WORDS_PER_SECOND = 2.65

# The model overshoots a stated word budget, so the budget it is GIVEN is
# scaled down from the true target. On the same measurement it produced 194
# words against a stated 140 — 38% over. Padding the video absorbs the
# remainder, but a held frame is a poor substitute for an animation that ran to
# the end of its own narration.
_BUDGET_OVERSHOOT_ALLOWANCE = 1.35

_NARRATION_SYSTEM = """You write the spoken narration for a short educational \
animation. Your words are read aloud over the animation by a text-to-speech \
voice; the student hears you and watches the visual at the same time.

Rules:
- Output ONLY the words to be spoken. No headings, no stage directions, no \
markdown, no bullet points, no speaker labels, no timestamps.
- Narrate what is ON SCREEN, in the order it appears. The animation is the \
lesson; you are the voice explaining it, not a separate summary of the topic.
- Write out mathematics the way a lecturer says it aloud: "d y by d x", \
"x squared", "the integral from zero to one of", "n choose k". Never emit \
LaTeX, backslashes, carets or underscores — the voice will read them literally.
- Plain sentences a listener can follow at speed. No sentence longer than about \
25 words.
- HARD LIMIT: at most {word_budget} words. This is not a target to approach — \
the animation ends, and anything you write past it plays over a frozen frame. \
Cut detail rather than exceed it. Shorter is always fine.
- British spelling, and a calm explanatory tone. No greetings, no sign-off, \
no "in this video we will".
- Accuracy over polish. If the animation shows a step, say what that step does \
and why, not that it is "important" or "interesting"."""


# ── Beat-aligned narration ──────────────────────────────────────────────────
#
# One narration track sized only to the video's TOTAL length drifts: the voice
# reaches step three while step two is still animating, and whatever it
# overshoots plays over a frozen last frame. The client heard exactly that.
#
# So when the renderer recorded its beats (media_assets.beats), narration is
# written as lines that each START on a beat, every line is voiced on its own,
# and each is laid over its own stretch of video. Where a line is shorter than
# its stretch the animation carries on under silence; where it is longer, that
# stretch's last frame holds until the voice finishes. Either way the next line
# begins exactly as its own visual does, so drift cannot accumulate.

# Breathing room after each line before the next visual starts.
_LINE_GAP_SECONDS = 0.3

_ALIGNED_SYSTEM = """You write the spoken narration for a short educational \
animation, line by line, so that each line is heard while its part of the \
animation is on screen.

You are given the animation's BEATS: numbered, timed stretches of what is on \
screen, in order. Return JSON only, of the form:

{{"lines": [{{"from": 0, "text": "..."}}, {{"from": 3, "text": "..."}}]}}

Rules:
- "from" is the beat at which that line STARTS. The line plays until the next \
line's beat. The first line must start at beat 0; "from" values strictly increase.
- Group beats: a line usually covers several short beats. Start a new line \
where something new appears that deserves its own sentence.
- Each line fits its stretch: about {words_per_second} words per second of the \
beats it covers. A line that runs long freezes the picture until it ends, so \
err short.
- Narrate what is ON SCREEN in that stretch. Write mathematics as it is said \
aloud ("d y by d x", "x squared"); never LaTeX, carets or underscores.
- Plain sentences, British spelling, calm tone. No greetings, no "in this video".
- A beat that is only a pause can share the previous line or stay silent."""


def plan_segments(
    beats: list[dict], lines: list[dict], total_seconds: float | None = None
) -> list[dict]:
    """Turn the model's lines into contiguous ``{start, end, text}`` segments.

    Defensive, because the lines come from a model: out-of-range and repeated
    beat indices are dropped, the first line is pinned to the start, and the
    last segment runs to the end of the video. Every second of video belongs to
    exactly one segment, so the mux never drops or duplicates footage.
    """
    if not beats:
        return []
    by_start: dict[int, str] = {}
    for line in lines:
        try:
            index = int(line.get("from"))
        except (TypeError, ValueError):
            continue
        text = " ".join(str(line.get("text") or "").split())
        if 0 <= index < len(beats) and text and index not in by_start:
            by_start[index] = text
    if not by_start:
        return []

    starts = sorted(by_start)
    if starts[0] != 0:
        # Nothing may play before the first line's footage is accounted for.
        by_start[0] = by_start.pop(starts[0])
        starts = sorted(by_start)

    end_of_video = total_seconds or float(beats[-1]["end"])
    segments = []
    for i, index in enumerate(starts):
        start = float(beats[index]["start"])
        end = float(beats[starts[i + 1]]["start"]) if i + 1 < len(starts) else end_of_video
        segments.append({"start": start, "end": end, "text": by_start[index]})
    return segments


def aligned_filter(segments: list[dict], audio_seconds: list[float], fps: float) -> tuple[str, float]:
    """The ffmpeg filter graph laying each line over its own stretch of video.

    Returns ``(filter, total_seconds)``. Input 0 is the video, input i+1 the
    audio for segment i.

    ``fps`` is not decoration: ``tpad`` after ``trim`` silently does nothing
    unless the frame rate is re-established first (verified on ffmpeg 7.1), so
    without it an overrunning line would play over the NEXT visual — the exact
    drift this exists to remove.
    """
    parts, joins, total = [], [], 0.0
    for i, (segment, spoken) in enumerate(zip(segments, audio_seconds)):
        span = max(0.0, segment["end"] - segment["start"])
        hold = max(0.0, spoken + _LINE_GAP_SECONDS - span)
        length = span + hold
        video = (
            f"[0:v]trim=start={segment['start']:.3f}:end={segment['end']:.3f},"
            f"setpts=PTS-STARTPTS,fps={fps:g}"
        )
        if hold > 0.05:
            video += f",tpad=stop_mode=clone:stop_duration={hold:.3f}"
        parts.append(f"{video}[v{i}]")
        parts.append(
            f"[{i + 1}:a]aformat=sample_rates=44100:channel_layouts=mono,"
            f"apad=whole_dur={length:.3f},atrim=0:{length:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        joins.append(f"[v{i}][a{i}]")
        total += length
    parts.append(f"{''.join(joins)}concat=n={len(segments)}:v=1:a=1[v][a]")
    return ";".join(parts), total


def _probe_fps(path: Path) -> float:
    try:
        proc = subprocess.run(
            [_binary("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        num, _, den = proc.stdout.strip().partition("/")
        fps = float(num) / float(den or 1)
        return fps if 1 <= fps <= 120 else 30.0
    except (OSError, subprocess.SubprocessError, ValueError, ZeroDivisionError):
        return 30.0


async def build_aligned_lines(
    *,
    concept_key: str,
    topic: str | None,
    source_script: str,
    scene_code: str | None,
    beats: list[dict],
) -> list[dict]:
    """Ask the model for narration lines keyed to beats. [] on any failure."""
    from app.ai.rag.claude import generate_response
    from app.ai.rag.modes_sessions.service import safe_parse_json_with_retry

    # Same measured rate and overshoot allowance as the single-track budget.
    words_per_second = round(_WORDS_PER_SECOND / _BUDGET_OVERSHOOT_ALLOWANCE, 2)
    system = _ALIGNED_SYSTEM.format(words_per_second=words_per_second)

    beat_lines = "\n".join(
        f"{i}. {b['start']:.1f}s to {b['end']:.1f}s ({b['end'] - b['start']:.1f}s): {b.get('label', '')}"
        for i, b in enumerate(beats)
    )
    prompt = (
        f"Concept: {concept_key}\nTopic: {topic or 'not specified'}\n\n"
        f"BEATS:\n{beat_lines}\n\n"
        f"The lesson script this animation was built from:\n{source_script}\n"
    )
    if scene_code:
        prompt += (
            "\nThe animation source that was executed (for Manim, beat k is the "
            f"k-th self.play or self.wait call to run, loops included):\n{scene_code[:12000]}\n"
        )

    async def _ask() -> str:
        return await generate_response(
            prompt=prompt, mode="scene_generation",
            system_parts=[{"type": "text", "text": system}],
        )

    try:
        parsed = await safe_parse_json_with_retry(await _ask(), _ask, label="narration lines")
    except Exception as exc:  # noqa: BLE001 — fall back to the single-track path
        logger.warning("Aligned narration plan failed for %s: %s", concept_key, exc)
        return []
    lines = parsed.get("lines") if isinstance(parsed, dict) else None
    return [
        {"from": line.get("from"), "text": _clean_script(str(line.get("text") or ""))}
        for line in (lines or []) if isinstance(line, dict)
    ]


def mux_aligned(video_path: Path, segments: list[dict], audio_paths: list[Path], out_path: Path) -> bool:
    """Lay each line over its own stretch of video. True on success."""
    durations = [probe_duration(p) or 0.0 for p in audio_paths]
    graph, _ = aligned_filter(segments, durations, _probe_fps(video_path))

    cmd = [_binary("ffmpeg"), "-y", "-i", str(video_path)]
    for path in audio_paths:
        cmd += ["-i", str(path)]
    cmd += [
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", str(out_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("ffmpeg aligned mux failed: %s", exc)
        return False
    if proc.returncode != 0:
        logger.error("ffmpeg aligned mux exited %s: %s", proc.returncode, proc.stderr[-1500:])
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def _resolve_voice_id(course_id: str | None = None) -> str | None:
    """The voice this course's videos are narrated in.

    Delegates to voices.voice_for_course so a concept video and the Learn-mode
    lesson board for the same course cannot end up in different voices — which
    is exactly what two separate resolvers would drift into.
    """
    if NARRATION_VOICE_ID:
        return NARRATION_VOICE_ID
    return voice_for_course(course_id)


# Re-exported from the render package so there is ONE ffprobe implementation.
# Kept as module-level names because the mux path and its tests reference them
# here.
_binary = binary
probe_duration = _probe_duration


async def build_narration_script(
    *,
    concept_key: str,
    topic: str | None,
    source_script: str,
    scene_code: str | None,
    duration_seconds: float | None,
) -> str:
    """Ask the model for narration matching what the animation shows.

    The generated scene code is included when it exists. It is the only exact
    record of what ends up on screen — the lesson script is what the lecturer
    *asked* for, and the two diverge whenever the renderer simplified a step.
    Narrating from the request rather than the result is how you get a voice
    describing an equation the student cannot see.
    """
    from app.ai.rag.claude import generate_response

    # Scaled down by the measured overshoot so the delivered length lands near
    # the animation's own, rather than the budget being met and the audio still
    # running long.
    target_words = (duration_seconds or 120) * _WORDS_PER_SECOND
    budget = max(40, int(target_words / _BUDGET_OVERSHOOT_ALLOWANCE))
    system = _NARRATION_SYSTEM.format(word_budget=budget)

    prompt = (
        f"Concept: {concept_key}\n"
        f"Topic: {topic or 'not specified'}\n"
        f"Animation length: {round(duration_seconds or 120)} seconds\n\n"
        f"The lesson script this animation was built from:\n{source_script}\n"
    )
    if scene_code:
        # Truncated: a long Manim scene is mostly positioning calls, and the
        # narration only needs the text and equations that appear.
        prompt += f"\nThe animation source that was executed:\n{scene_code[:12000]}\n"

    raw = await generate_response(
        prompt=prompt,
        mode="scene_generation",
        system_parts=[{"type": "text", "text": system}],
    )
    return _clean_script(raw)


def _clean_script(raw: str) -> str:
    """Strip the scaffolding models add despite being told not to."""
    text = (raw or "").strip()

    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    cleaned: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        # Stage directions and headings the voice must not read out.
        if stripped.startswith(("#", "**[", "[", "(pause", "Narrator:", "NARRATION")):
            continue
        cleaned.append(stripped.lstrip("-*• ").strip())

    return " ".join(part for part in cleaned if part).strip()


def synthesize(text: str, voice_id: str) -> bytes:
    from app.media.tts_service import tts_to_mp3_bytes

    return tts_to_mp3_bytes(text, voice_id=voice_id)


def mux(video_path: Path, audio_path: Path, out_path: Path) -> bool:
    """Lay the narration over the animation. True on success.

    The video is padded with a held final frame rather than the audio being
    truncated. Narration that runs a few seconds past the last animation step
    is normal — TTS pacing is not exact — and cutting it mid-sentence is much
    worse than three seconds of a still frame. ``-shortest`` would do exactly
    that, which is why it is not used here.
    """
    video_seconds = probe_duration(video_path) or 0
    audio_seconds = probe_duration(audio_path) or 0
    overhang = max(0.0, audio_seconds - video_seconds)

    cmd = [_binary("ffmpeg"), "-y", "-i", str(video_path), "-i", str(audio_path)]

    if overhang > 0.25:
        # Padding is the right call — truncating a sentence is worse — but a
        # long hold means the word budget is not landing, and that is a prompt
        # problem, not a muxing one. Logged so it is visible in the render logs
        # instead of only being visible to whoever watches the video.
        if video_seconds and overhang > video_seconds * 0.15:
            logger.warning(
                "Narration overran the animation by %.1fs (%.0f%% of a %.0fs video); "
                "the last frame will be held. Check the word budget in "
                "build_narration_script.",
                overhang, 100 * overhang / video_seconds, video_seconds,
            )
        cmd += ["-vf", f"tpad=stop_mode=clone:stop_duration={overhang + 0.5:.2f}"]
        # Re-encoding is forced by the filter; without a filter the copy below
        # keeps the render bit-for-bit.
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    else:
        cmd += ["-c:v", "copy"]

    cmd += ["-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0", str(out_path)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("ffmpeg mux failed: %s", exc)
        return False

    if proc.returncode != 0:
        logger.error("ffmpeg mux exited %s: %s", proc.returncode, proc.stderr[-1500:])
        return False

    return out_path.exists() and out_path.stat().st_size > 0


def _mark(asset_id: str, **fields) -> None:
    try:
        get_supabase().table("media_assets").update(fields).eq("id", asset_id).execute()
    except Exception as exc:  # noqa: BLE001 — sql/013 may not be applied yet
        logger.warning("Could not update narration state for %s: %s", asset_id, exc)


async def narrate_asset(asset_id: str) -> dict:
    """Add a spoken track to one rendered asset, in place.

    Every failure path leaves the silent video exactly as it was and records
    why. A video without narration is a usable teaching artefact; losing the
    render because the TTS provider was down would not be.
    """
    from app.media.render.service import RENDERED_MEDIA_BUCKET

    sb = get_supabase()
    rows = sb.table("media_assets").select("*").eq("id", asset_id).execute().data
    if not rows:
        raise LookupError(f"No media asset {asset_id}")
    asset = rows[0]

    if not NARRATION_ENABLED:
        _mark(asset_id, narration_status="skipped")
        return {"asset_id": asset_id, "status": "skipped", "reason": "narration disabled"}

    if asset.get("status") != "ready" or not asset.get("storage_path"):
        _mark(asset_id, narration_status="skipped")
        return {"asset_id": asset_id, "status": "skipped", "reason": "asset is not ready"}

    if asset.get("has_audio"):
        return {"asset_id": asset_id, "status": "ready", "reason": "already narrated"}

    voice_id = _resolve_voice_id(asset.get("course_id"))
    if not voice_id:
        _mark(asset_id, narration_status="failed")
        return {"asset_id": asset_id, "status": "failed", "reason": "no narration voice configured"}

    _mark(asset_id, narration_status="narrating")

    workdir = Path(tempfile.mkdtemp(prefix="narration-"))
    try:
        video_path = workdir / "video.mp4"
        try:
            video_path.write_bytes(
                sb.storage.from_(RENDERED_MEDIA_BUCKET).download(asset["storage_path"])
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not download %s for narration: %s", asset_id, exc)
            _mark(asset_id, narration_status="failed")
            return {"asset_id": asset_id, "status": "failed", "reason": "download failed"}

        duration = probe_duration(video_path) or asset.get("duration_seconds")
        out_path = workdir / "narrated.mp4"

        beats = asset.get("beats")
        if isinstance(beats, str):
            beats = json.loads(beats)
        if isinstance(beats, list) and len(beats) >= 2:
            aligned = await _narrate_aligned(asset, beats, duration, voice_id, video_path, out_path, workdir)
            if aligned:
                return await _finish(asset, aligned, out_path, duration)
            # Fall through: a single track is worse, but far better than silence.
            logger.warning("Aligned narration failed for %s; using a single track", asset_id)

        script = await build_narration_script(
            concept_key=asset.get("concept_key") or "",
            topic=asset.get("topic"),
            source_script=asset.get("source_script") or "",
            scene_code=asset.get("scene_code"),
            duration_seconds=duration,
        )
        if not script:
            _mark(asset_id, narration_status="failed")
            return {"asset_id": asset_id, "status": "failed", "reason": "empty narration script"}

        try:
            audio = synthesize(script, voice_id)
        except Exception as exc:  # noqa: BLE001 — provider outage, bad key, quota
            logger.error("TTS failed for asset %s: %s", asset_id, exc)
            # The script is stored even though the audio failed: it is the
            # expensive half, and a retry should not pay for it twice.
            _mark(asset_id, narration_status="failed", narration_script=script)
            return {"asset_id": asset_id, "status": "failed", "reason": "text-to-speech failed"}

        audio_path = workdir / "narration.mp3"
        audio_path.write_bytes(audio)

        if not mux(video_path, audio_path, out_path):
            _mark(asset_id, narration_status="failed", narration_script=script)
            return {"asset_id": asset_id, "status": "failed", "reason": "could not mux audio"}

        return await _finish(asset, script, out_path, duration)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _narrate_aligned(
    asset: dict, beats: list[dict], duration: float | None, voice_id: str,
    video_path: Path, out_path: Path, workdir: Path,
) -> str | None:
    """Beat-aligned narration into ``out_path``. Returns the script, or None."""
    lines = await build_aligned_lines(
        concept_key=asset.get("concept_key") or "",
        topic=asset.get("topic"),
        source_script=asset.get("source_script") or "",
        scene_code=asset.get("scene_code"),
        beats=beats,
    )
    segments = plan_segments(beats, lines, duration)
    if not segments:
        return None

    try:
        # Lines are independent requests; a handful at once keeps a 10-line
        # video from waiting on ten sequential round trips.
        with ThreadPoolExecutor(max_workers=4) as pool:
            clips = list(pool.map(lambda seg: synthesize(seg["text"], voice_id), segments))
    except Exception as exc:  # noqa: BLE001 — provider outage, bad key, quota
        logger.error("TTS failed for aligned narration of %s: %s", asset.get("id"), exc)
        return None

    audio_paths = []
    for i, clip in enumerate(clips):
        path = workdir / f"line-{i:02d}.mp3"
        path.write_bytes(clip)
        audio_paths.append(path)

    if not mux_aligned(video_path, segments, audio_paths, out_path):
        return None
    return "\n".join(seg["text"] for seg in segments)


async def _finish(asset: dict, script: str, out_path: Path, duration: float | None) -> dict:
    from app.media.storage_service import upload_rendered_media

    asset_id = asset["id"]
    path = upload_rendered_media(
        asset["course_id"], asset_id, out_path.read_bytes(), "video/mp4", "mp4"
    )
    if not path:
        _mark(asset_id, narration_status="failed", narration_script=script)
        return {"asset_id": asset_id, "status": "failed", "reason": "upload failed"}

    _mark(
        asset_id,
        narration_status="ready",
        narration_script=script,
        has_audio=True,
        storage_path=path,
        duration_seconds=probe_duration(out_path) or duration,
    )
    logger.info("Narration ready  asset=%s  words=%d", asset_id, len(script.split()))
    return {"asset_id": asset_id, "status": "ready", "words": len(script.split())}
