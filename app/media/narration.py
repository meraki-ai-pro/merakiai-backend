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

import logging
import os
import shutil
import subprocess
import tempfile
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
    from app.media.storage_service import upload_rendered_media

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

        out_path = workdir / "narrated.mp4"
        if not mux(video_path, audio_path, out_path):
            _mark(asset_id, narration_status="failed", narration_script=script)
            return {"asset_id": asset_id, "status": "failed", "reason": "could not mux audio"}

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
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
