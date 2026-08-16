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
import sys
import tempfile
from pathlib import Path

from app.ai.rag.claude import generate_response
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
- Build the idea in steps with self.play(...), and self.wait(1) between steps \
so a student can read each one.
- Keep the whole animation under {duration} seconds.
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


def _find_output(media_dir: Path) -> Path | None:
    """Locate the rendered mp4. Manim nests it under quality-named folders."""
    candidates = sorted(
        media_dir.rglob("*.mp4"), key=lambda p: p.stat().st_size, reverse=True
    )
    return candidates[0] if candidates else None


def _render_env(workdir: Path) -> dict[str, str]:
    """The subprocess environment: the minimum manim needs, and nothing else.

    Deliberately excludes every credential the parent process holds. On Windows
    the minimum is larger than on Linux — CPython cannot initialise without
    SYSTEMROOT (os.urandom and the socket/ssl machinery reach into system DLLs),
    so `python -m manim` fails before manim's own code runs. Those variables
    name system paths, not secrets.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    if sys.platform == "win32":
        env.update({
            "TEMP": str(workdir),
            "TMP": str(workdir),
            "USERPROFILE": str(workdir),
        })
        for name in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATHEXT", "COMSPEC",
                     "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"):
            value = os.environ.get(name)
            if value:
                env[name] = value

    return env


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
            timeout=RENDER_TIMEOUT_SECONDS,
            # An empty environment except for what manim genuinely needs. The
            # parent process holds ANTHROPIC_API_KEY, SUPABASE keys and the
            # Pinecone key; none of them should be readable by generated code.
            env=_render_env(workdir),
        )
    except subprocess.TimeoutExpired:
        return 124, f"Render exceeded the {RENDER_TIMEOUT_SECONDS}s time limit."

    return proc.returncode, f"{proc.stdout}\n{proc.stderr}".strip()


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
                return output[-2000:] or f"manim exited with status {code}"

            video = _find_output(workdir / "media")
            if not video:
                return "manim reported success but produced no video file."

            return RenderResult(
                content=video.read_bytes(),
                media_type="video/mp4",
                extension="mp4",
                scene_code=source,
            )
        finally:
            # The workdir holds generated code and intermediate frames; a
            # failed render must not leave either on disk.
            shutil.rmtree(workdir, ignore_errors=True)


register_lazy("manim", ManimRenderer)
