"""Celery entry point for the render worker.

Deliberately separate from app.ai.tasks. That module imports the video, TTS and
RAG stacks at module scope; importing it here would put ElevenLabs, D-ID,
Pinecone and OpenAI clients inside the container that runs LLM-generated code,
for no benefit. The render worker sets CELERY_INCLUDE to this module instead.

The API never imports this file — it dispatches by task name.
"""

from __future__ import annotations

import asyncio
import logging
import os

from celery import shared_task

logger = logging.getLogger(__name__)

# Registers the renderer, at module scope because this module is only ever
# loaded inside a render container.
#
# Only the renderer THIS image carries is imported: the Manim image has no
# Node or Chromium and the Remotion image has no manim or LaTeX, so importing
# both would break whichever container is running.
_RENDERER = os.getenv("RENDERER", "manim").strip().lower()

if _RENDERER == "remotion":
    import app.media.render.remotion_renderer  # noqa: E402,F401
else:
    import app.media.render.manim_renderer  # noqa: E402,F401


@shared_task(name="app.media.render.tasks.process_render_task")
def process_render_task(asset_id: str):
    """Render one media asset.

    Never raises: a failure is recorded on the asset row and surfaced to the
    lecturer's review queue, which is more useful than a dead-lettered task
    nobody looks at.
    """
    from app.db.supabase import reset_async_supabase
    from app.media.render.service import execute_render

    reset_async_supabase()
    try:
        return asyncio.run(execute_render(asset_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("process_render_task failed  asset=%s: %s", asset_id, exc)
        try:
            from app.media.render.service import mark

            mark(asset_id, status="failed", error=str(exc)[:2000])
        except Exception:  # noqa: BLE001 — nothing left to do but log
            logger.exception("Could not record render failure for %s", asset_id)
        return {"asset_id": asset_id, "status": "failed", "error": str(exc)[:300]}
