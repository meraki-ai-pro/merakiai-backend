"""Remotion renderer: lesson script -> composed explainer video.

Serves the archetypes Manim is bad at — charts built from data, process flows,
captioned sequences, and algorithm or data-structure walkthroughs, which are
easier in React because you can render the real DOM of the structure.

Unlike the Manim path there is **no sandbox**, because there is nothing to
sandbox: the model produces JSON matching remotion_spec.RemotionSpec and a
fixed set of React components renders it. No generated code is executed at any
point. See remotion_spec for why that distinction is available here and not for
Manim.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import ValidationError

from app.ai.rag.claude import generate_response
from app.media.render.child_env import render_env, resolve_executable
from app.media.render.registry import RenderRequest, RenderResult, register_lazy
from app.media.render.remotion_spec import RemotionSpec, validate_spec

logger = logging.getLogger(__name__)

RENDER_TIMEOUT_SECONDS = int(os.getenv("REMOTION_RENDER_TIMEOUT", "900"))

# Where the Remotion project lives inside the render container.
PROJECT_DIR = Path(os.getenv("REMOTION_PROJECT", "/app/remotion"))

_MAX_REPAIR_ATTEMPTS = int(os.getenv("REMOTION_REPAIR_ATTEMPTS", "1"))

FPS = 30

_SPEC_SYSTEM = """You describe an explainer video as JSON. You do not write code.

Output ONLY a JSON object. No markdown fences, no commentary.

Schema:
{
  "archetype": "data_story" | "process_flow" | "composited_explainer" | "timeline" | "ui_or_code_walkthrough",
  "title": "<= 120 chars",
  "subtitle": "<= 160 chars, optional",
  "slides": [{"title": "<= 120", "body": "<= 240, optional", "seconds": 3-15}],
  "steps": [{"label": "<= 80", "detail": "<= 240, optional"}],
  "chart": {
    "kind": "bar" | "line" | "area",
    "x_labels": ["..."],
    "series": [{"name": "...", "values": [1, 2, 3]}],
    "x_title": "optional", "y_title": "optional"
  },
  "accent": "#rrggbb"
}

Rules:
- data_story REQUIRES chart. process_flow and timeline REQUIRE steps.
  composited_explainer and ui_or_code_walkthrough REQUIRE slides.
- Every series must have exactly as many values as there are x_labels.
- At most 8 slides or steps. Keep the whole video under 3 minutes.
- Pace it for someone meeting this for the first time. Give a slide enough seconds to READ its body aloud twice — 5 to 8 for anything with a body. Fewer, slower slides beat more, faster ones.
- Write for a student who has not seen this before. Plain sentences.
- Any field not in this schema is discarded, so do not invent one."""


def _strip_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    # Models sometimes prepend a sentence before the object.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start > 0 and end > start:
        cleaned = cleaned[start : end + 1]
    return cleaned


async def generate_spec(request: RenderRequest, feedback: str | None = None) -> RemotionSpec:
    """Ask the model for a spec, then validate it against the schema."""
    prompt = (
        f"Concept: {request.concept_key}\n"
        f"Topic: {request.topic or 'not specified'}\n"
        f"Suggested archetype: {request.archetype or 'choose the best fit'}\n\n"
        f"Lesson script:\n{request.source_script}\n"
    )
    if feedback:
        prompt += f"\nYour previous attempt was rejected: {feedback}\nFix it."

    raw = await generate_response(
        prompt=prompt,
        mode="scene_generation",
        system_parts=[{"type": "text", "text": _SPEC_SYSTEM}],
    )
    return validate_spec(json.loads(_strip_fences(raw)))


def _render_spec(spec: RemotionSpec) -> RenderResult | str:
    """Invoke the Remotion CLI. Returns the result, or an error string."""
    if not PROJECT_DIR.exists():
        return (
            f"Remotion project not found at {PROJECT_DIR}. "
            "Build the remotion render image (remotion.Dockerfile)."
        )

    workdir = Path(tempfile.mkdtemp(prefix="remotion-render-"))
    try:
        props_path = workdir / "props.json"
        # WRAPPED in {"spec": ...}, because that is the composition's prop
        # shape (Root.tsx: defaultProps={{ spec }}). Writing the spec at the
        # top level meant Remotion merged nothing over `spec`, fell back to
        # DEFAULT_SPEC and rendered a placeholder reading "No spec supplied" —
        # silently, whenever the frame count happened to line up.
        props_path.write_text(
            json.dumps({"spec": spec.model_dump(mode="json")}), encoding="utf-8"
        )
        out_path = workdir / "out.mp4"

        frames = max(int(spec.duration_seconds * FPS), FPS)

        # Resolved to an absolute path in the PARENT, where PATHEXT is
        # intact. On Windows `npx` is `npx.cmd`, and a child handed the bare
        # name with a trimmed environment fails with "npx not found" — which
        # reads as a missing toolchain rather than a missing env var.
        cmd = [
            resolve_executable("npx"), "--no-install", "remotion", "render",
            "src/index.ts", "Lesson", str(out_path),
            f"--props={props_path}",
            f"--frames=0-{frames - 1}",
            "--concurrency=1",
            # Chromium in a container has no usable /dev/shm and no sandbox.
            "--gl=swiftshader",
        ]

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(PROJECT_DIR),
                capture_output=True,
                text=True,
                # Remotion writes ANSI colour and box-drawing characters. On a
                # Windows console the default cp1252 decode raises inside
                # subprocess's reader THREAD, which surfaces as an unrelated
                # traceback rather than as a render failure.
                encoding="utf-8",
                errors="replace",
                timeout=RENDER_TIMEOUT_SECONDS,
                # Same reasoning as the Manim renderer, and now the same
                # implementation: the parent holds the Anthropic and Supabase
                # keys and a render needs neither. render_env also supplies the
                # Windows system variables Node cannot start without.
                env=render_env(workdir, {
                    "NODE_ENV": "production",
                    "REMOTION_DISABLE_TELEMETRY": "1",
                }),
            )
        except subprocess.TimeoutExpired:
            return f"Render exceeded the {RENDER_TIMEOUT_SECONDS}s time limit."
        except FileNotFoundError:
            return "npx not found — this renderer only runs in the Remotion container."

        if proc.returncode != 0:
            return f"{proc.stdout}\n{proc.stderr}".strip()[-2000:]

        if not out_path.exists():
            return "Remotion reported success but produced no file."

        return RenderResult(
            content=out_path.read_bytes(),
            media_type="video/mp4",
            extension="mp4",
            duration_seconds=spec.duration_seconds,
            # The spec, not code — this is what a lecturer reviews and what
            # reproduces the render.
            scene_code=spec.model_dump_json(indent=2),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class RemotionRenderer:
    name = "remotion"

    async def render(self, request: RenderRequest) -> RenderResult:
        feedback: str | None = None
        last_error = ""

        for attempt in range(_MAX_REPAIR_ATTEMPTS + 1):
            try:
                spec = await generate_spec(request, feedback)
            except (ValidationError, json.JSONDecodeError) as exc:
                # A schema miss, not an attack — there is no code here to be
                # malicious with. One correction attempt, then give up.
                last_error = str(exc)[:500]
                feedback = last_error
                logger.warning(
                    "Remotion spec rejected (attempt %d) for %s: %s",
                    attempt + 1, request.concept_key, last_error,
                )
                continue

            result = await asyncio.to_thread(_render_spec, spec)
            if isinstance(result, RenderResult):
                return result

            last_error = result
            feedback = f"The render failed: {result[:400]}"
            logger.warning("Remotion render failed (attempt %d): %s", attempt + 1, result[:300])

        raise RuntimeError(f"Could not produce a video for {request.concept_key!r}: {last_error}")


register_lazy("remotion", RemotionRenderer)
