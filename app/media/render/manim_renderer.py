"""Manim renderer: lesson script -> animated concept video.

Serves the four pilot courses directly — Calculus, Statistics and Probability,
Quantitative Techniques, General Mathematics — plus physics and engineering.
Manim's strength is that the animation *is* the derivation: an equation
rearranging term by term, a tangent sweeping along a curve, a shaded area
converging on an integral.

Two-layer safety, and both are required:

  1. :mod:`app.media.render.sandbox` statically rejects generated code that
     reaches outside a drawing, and
  2. this module executes it as a subprocess under a hard wall-clock timeout,
     in a working directory it cannot escape.

Layer 2 is only *complete* when the process runs inside the render container
(render.Dockerfile: no network, read-only root, non-root user, memory cap).
Never point RENDER_ALLOW_LOCAL at a machine you care about.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.ai.rag.claude import generate_response
from app.media.render.child_env import render_env
from app.media.render.media_probe import binary, probe_duration
from app.media.render.registry import RenderRequest, RenderResult, register_lazy
from app.media.render.sandbox import UnsafeSceneError, validate_scene

logger = logging.getLogger(__name__)

# A 3-minute animation should never take longer than this to draw. Anything
# that does is either pathological or looping, and the queue must not be held.
RENDER_TIMEOUT_SECONDS = int(os.getenv("MANIM_RENDER_TIMEOUT", "600"))

# Quality flag. -qm (720p30) renders several times faster than -qh and is
# indistinguishable on a phone, which is what the pilot cohort will use.
MANIM_QUALITY = os.getenv("MANIM_QUALITY", "-qm")

# Which interpreter runs manim. Defaults to this process's own, which is right
# inside the render container. Outside it, manim (and its cairo/pango/LaTeX
# stack) is usually installed in a separate environment from the API's, so
# point MANIM_PYTHON at that interpreter rather than installing a renderer's
# worth of dependencies into the backend venv.
MANIM_PYTHON = os.getenv("MANIM_PYTHON") or sys.executable

_MAX_REPAIR_ATTEMPTS = int(os.getenv("MANIM_REPAIR_ATTEMPTS", "1"))

# Playback stretch applied to the finished video, as a fraction of real time:
# 0.85 plays it back 15% slower. The prompt above asks for humane pacing, but a
# model reliably drifts back towards one-second animations and half-second
# pauses — the chain-rule render shipped 26 animations with no run_time at all,
# a visual change every 2.3 seconds. This is the deterministic half of the fix
# and needs no cooperation from the model.
#
# Uniform, so nothing desynchronises: narration is generated afterwards from
# the PROBED duration of the stretched file.
#
# Set MANIM_SPEED=1.0 to disable.
MANIM_SPEED = float(os.getenv("MANIM_SPEED", "0.85"))


_SCENE_SYSTEM = """You write Manim Community Edition scenes that teach one \
mathematical concept clearly.

Rules:
- Output ONLY Python code. No markdown fences, no commentary, no explanation.
- Define exactly ONE class deriving from Scene.
- Import only from: manim, math, numpy, random, itertools, fractions, decimal.
- Never import os, sys, subprocess or anything touching the filesystem or \
network. Never use eval, exec, open, globals, getattr or dunder attributes. \
Code doing any of that is rejected before it runs.
- Use MathTex for mathematics and Text for prose. Escape backslashes correctly.
- The LaTeX preamble provides amsmath, amssymb, mathrsfs and xcolor only. Do \
NOT use commands from physics, calligra, wasysym, dsfont or ragged2e \
(\\dv, \\qty, \\mathds, \\Centering and similar) — they will fail to compile.
- PACE IT FOR SOMEONE MEETING THIS FOR THE FIRST TIME. Generated lessons are \
almost always TOO FAST to follow, and that is the single most common complaint \
about them.
  * Give EVERY self.play(...) an explicit run_time of at least 1.5 — use 2 to \
2.5 for anything introducing new notation.
  * Follow every step with self.wait(2), and self.wait(3) after a new equation, \
a substitution, or any line the student has to read before the next change.
  * Change ONE thing at a time. Two or three simultaneous transforms are \
unreadable however long they last.
  * Fewer, slower steps beat more, faster ones. A student who cannot keep up \
learns nothing from the extra content.
- Aim for roughly {duration} seconds and treat that as room to breathe, not a \
budget to fill. Running short is fine; rushing to fit more in is not.
- Keep every object inside the frame; prefer VGroup(...).arrange() over \
hand-positioned coordinates.
- Mathematical accuracy matters more than visual flourish. A wrong sign in a \
lecture hall is the failure mode that matters."""


def _strip_fences(text: str) -> str:
    """Remove markdown fences the model adds despite being told not to."""
    cleaned = (text or "").strip()
    if not cleaned.startswith("```"):
        return cleaned

    lines = cleaned.splitlines()
    lines = lines[1:]  # drop the opening fence (and any language tag)
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def generate_scene(request: RenderRequest, feedback: str | None = None) -> str:
    """Ask the model for a scene, then validate it. Returns the source."""
    prompt = (
        f"Concept: {request.concept_key}\n"
        f"Topic: {request.topic or 'not specified'}\n\n"
        f"Lesson script to animate:\n{request.source_script}\n"
    )
    if feedback:
        prompt += (
            f"\nYour previous attempt was rejected: {feedback}\n"
            "Produce a corrected scene that does not repeat the problem."
        )

    system = _SCENE_SYSTEM.format(duration=request.duration_hint_seconds)
    # "scene_generation", not "learn": the learn config caps at 1000 tokens,
    # which truncates a scene mid-statement and fails to parse.
    raw = await generate_response(
        prompt=prompt,
        mode="scene_generation",
        system_parts=[{"type": "text", "text": system}],
    )
    return _strip_fences(raw)


# Manim's default tex template loads calligra, physics, wasysym, dsfont and
# ragged2e. Those live in texlive-fonts-extra and texlive-latex-extra — roughly
# 800MB of the 1069MB the documented package set downloads — and none of them
# is used by ordinary mathematical notation.
#
# This replaces it with what the pilot's content actually needs: amsmath and
# amssymb for operators, relations and symbols, mathrsfs for script letters,
# xcolor because Manim colours sub-expressions.
#
# "YourTextHere" is Manim's substitution placeholder and must be preserved.
_MINIMAL_TEX_TEMPLATE = r"""\documentclass[preview]{standalone}
\usepackage[english]{babel}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{mathrsfs}
\usepackage{xcolor}
\linespread{1}
\begin{document}

YourTextHere

\end{document}
"""

# Points manim at the template above. Written next to the scene so it applies
# to this render only, and so the generated code is never modified after the
# sandbox has validated it.
_MANIM_CFG = """[CLI]
tex_template_file = tex_template.tex
"""


def _slow_down(video: Path, speed: float) -> Path:
    """Stretch playback so a first-time viewer can keep up.

    Returns the original path unchanged when no stretch is wanted or ffmpeg
    fails — a video that plays slightly fast is far better than no video, so
    this never turns a successful render into a failed one.

    Video-only: a Manim render carries no audio track. Narration is added
    later, by a different worker, from the probed duration of whatever this
    returns, so the two cannot drift apart.
    """
    if speed >= 0.999:
        return video

    out = video.with_name(f"{video.stem}-paced.mp4")
    cmd = [
        binary("ffmpeg"), "-y", "-i", str(video),
        # setpts multiplies each frame's timestamp: 1/0.85 spaces them further
        # apart, so the same frames play over a longer period. No re-timing of
        # the animation itself and no frames dropped or invented.
        "-filter:v", f"setpts={1 / speed:.4f}*PTS",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        str(out),
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=RENDER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not slow the render down (%s); shipping it as rendered", exc)
        return video

    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        logger.warning(
            "ffmpeg refused to slow the render down; shipping it as rendered: %s",
            proc.stderr[-400:],
        )
        return video

    logger.info("Paced render to %.0f%% speed", speed * 100)
    return out


def _find_output(media_dir: Path) -> Path | None:
    """Locate the rendered mp4. Manim nests it under quality-named folders."""
    candidates = sorted(
        media_dir.rglob("*.mp4"), key=lambda p: p.stat().st_size, reverse=True
    )
    return candidates[0] if candidates else None


def _render_env(workdir: Path) -> dict[str, str]:
    """The subprocess environment: the minimum manim needs, and nothing else.

    Delegates to child_env.render_env, which both renderers share — Remotion
    hit exactly the same Windows problem this function was written to fix, so
    the rule now lives in one place.
    """
    return render_env(workdir, {"PYTHONDONTWRITEBYTECODE": "1"})


def _latex_diagnostic(workdir: Path) -> str:
    """Return the useful part of Manim's newest LaTeX compiler log.

    Manim's console traceback only says that conversion to DVI failed.  The
    actual bad command or delimiter is written to ``media/Tex/*.log``.  Keep
    the error and a little surrounding context so the model's repair attempt
    can correct the expression instead of seeing the same generic traceback.
    """
    tex_dir = workdir / "media" / "Tex"
    try:
        logs = sorted(
            tex_dir.glob("*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return ""

    if not logs:
        return ""

    try:
        lines = logs[0].read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    markers = (
        "! ",
        "latex error",
        "undefined control sequence",
        "missing $",
        "missing }",
        "extra }",
        "double superscript",
        "double subscript",
        "runaway argument",
        "emergency stop",
    )
    interesting = [
        index
        for index, line in enumerate(lines)
        if line.lstrip().startswith("!")
        or line.lstrip().startswith("l.")
        or any(marker in line.lower() for marker in markers)
    ]
    if not interesting:
        return ""

    selected: list[str] = []
    seen: set[int] = set()
    for index in interesting[:8]:
        for nearby in range(max(0, index - 1), min(len(lines), index + 3)):
            if nearby not in seen:
                seen.add(nearby)
                selected.append(lines[nearby])

    detail = "\n".join(selected).strip()
    if not detail:
        return ""
    return f"LaTeX compiler diagnostic:\n{detail}"[:2500]


def _run_manim(scene_path: Path, scene_name: str, workdir: Path) -> tuple[int, str]:
    """Execute manim as a subprocess. Returns (exit code, combined output)."""
    import subprocess

    cmd = [
        MANIM_PYTHON, "-m", "manim",
        MANIM_QUALITY,
        "--disable_caching",          # cache reuse across scenes is a leak vector
        "--media_dir", str(workdir / "media"),
        str(scene_path),
        scene_name,
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            # Same reason as the Remotion renderer: manim's progress bars and
            # rich-formatted errors are not cp1252, and the decode happens on a
            # reader thread where the failure is hard to attribute.
            encoding="utf-8",
            errors="replace",
            timeout=RENDER_TIMEOUT_SECONDS,
            # An empty environment except for what manim genuinely needs. The
            # parent process holds ANTHROPIC_API_KEY, SUPABASE keys and the
            # Pinecone key; none of them should be readable by generated code.
            env=_render_env(workdir),
        )
    except subprocess.TimeoutExpired:
        return 124, f"Render exceeded the {RENDER_TIMEOUT_SECONDS}s time limit."

    output = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0:
        diagnostic = _latex_diagnostic(workdir)
        if diagnostic:
            output = f"{diagnostic}\n\nRenderer output (tail):\n{output[-1000:]}"
    return proc.returncode, output


class ManimRenderer:
    name = "manim"

    async def render(self, request: RenderRequest) -> RenderResult:
        feedback: str | None = None
        last_error = ""

        for attempt in range(_MAX_REPAIR_ATTEMPTS + 1):
            source = await generate_scene(request, feedback)

            try:
                scene_name = validate_scene(source)
            except UnsafeSceneError as exc:
                # Not necessarily an attack — models produce `import os` out of
                # habit. One repair attempt, then give up rather than looping.
                logger.warning(
                    "Generated scene rejected (attempt %d) for %s: %s",
                    attempt + 1, request.concept_key, exc,
                )
                last_error = str(exc)
                feedback = str(exc)
                continue

            result = await asyncio.to_thread(self._render_source, source, scene_name)
            if isinstance(result, RenderResult):
                return result

            last_error = result
            feedback = f"The scene failed to render: {result[:500]}"
            logger.warning(
                "Manim render failed (attempt %d) for %s: %s",
                attempt + 1, request.concept_key, result[:300],
            )

        raise RuntimeError(f"Could not produce a scene for {request.concept_key!r}: {last_error}")

    def _render_source(self, source: str, scene_name: str) -> RenderResult | str:
        """Render validated source. Returns the result, or an error string."""
        workdir = Path(tempfile.mkdtemp(prefix="manim-render-"))
        try:
            scene_path = workdir / "scene.py"
            scene_path.write_text(source, encoding="utf-8")

            # Written as sibling files rather than injected into the scene, so
            # what runs is byte-for-byte what validate_scene() approved.
            (workdir / "tex_template.tex").write_text(_MINIMAL_TEX_TEMPLATE, encoding="utf-8")
            (workdir / "manim.cfg").write_text(_MANIM_CFG, encoding="utf-8")

            code, output = _run_manim(scene_path, scene_name, workdir)
            if code != 0:
                return output[:2000] or f"manim exited with status {code}"

            video = _find_output(workdir / "media")
            if not video:
                return "manim reported success but produced no video file."

            video = _slow_down(video, MANIM_SPEED)

            return RenderResult(
                content=video.read_bytes(),
                media_type="video/mp4",
                extension="mp4",
                # Probed from the file rather than predicted from the scene.
                # Manim decides the real length (self.wait calls, animation
                # run_times), and leaving this None meant every Manim video
                # showed no duration in the lecturer's review queue while
                # Remotion ones did.
                duration_seconds=probe_duration(video),
                scene_code=source,
            )
        finally:
            # The workdir holds generated code and intermediate frames; a
            # failed render must not leave either on disk.
            shutil.rmtree(workdir, ignore_errors=True)


register_lazy("manim", ManimRenderer)
