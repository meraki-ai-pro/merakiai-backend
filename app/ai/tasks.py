import asyncio
import base64
import json
import logging
import os
import time

import redis as redis_sync
from celery import shared_task

logger = logging.getLogger(__name__)

from app.core.chat_repo import (
    load_memory,
    response_format_for_mode,
    log_conversation,
    get_session_course_id,
    get_course_info,
)
from app.ai.rag.service import query_rag
from app.media.video_service import maybe_generate_video
from app.media.storage_service import STUDENT_UPLOADS_BUCKET, upload_student_image
from app.db.supabase import get_supabase, reset_async_supabase
from app.core import analytics, events


# ---------------------------------------------------------------------------
# Redis singleton — shared across all tasks in this worker process (M-12).
# Eliminates the per-publish TCP connection that the old from_url()/close()
# pattern created.
# ---------------------------------------------------------------------------

_redis_client: redis_sync.Redis | None = None


def _get_redis() -> redis_sync.Redis:
    global _redis_client
    if _redis_client is None:
        pool = redis_sync.ConnectionPool.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            max_connections=10,
        )
        _redis_client = redis_sync.Redis(connection_pool=pool)
    return _redis_client


# ---------------------------------------------------------------------------
# Redis Pub/Sub delivery helper
# ---------------------------------------------------------------------------

def _publish_to_ws(session_id: str, result: dict) -> None:
    """Publish task result to Redis Pub/Sub for WebSocket delivery."""
    try:
        _get_redis().publish(f"ws:{session_id}", json.dumps(result))
    except Exception as exc:
        logger.error(
            "Failed to publish WS result  session=%s  error=%s", session_id, exc
        )


def _board_enabled() -> bool:
    """Lesson-board presentation for Learn answers. Set LESSON_BOARD=0 to disable."""
    return os.getenv("LESSON_BOARD", "1").strip().lower() not in ("0", "false", "off", "no")


def _publish_status(session_id: str, stage: str, label: str) -> None:
    """Emit a progress event that drives the "what's happening" panel in the UI."""
    _publish_to_ws(session_id, {"type": "status", "stage": stage, "label": label})


async def _stream_mode_text(
    session_id: str,
    *,
    mode: str,
    message_type: str,
    content: str,
    step: int | None = None,
    total_steps: int | None = None,
) -> None:
    """Stream validated practice/review display text to the UI."""
    _publish_to_ws(
        session_id,
        {
            "type": "mode_text_stream_start",
            "mode": mode,
            "message_type": message_type,
            "step": step,
            "total_steps": total_steps,
        },
    )

    chunk = ""
    for part in content.split(" "):
        next_chunk = f"{part} "
        if len(chunk) + len(next_chunk) > 48:
            _publish_to_ws(session_id, {"type": "mode_text_chunk", "chunk": chunk})
            await asyncio.sleep(0.01)
            chunk = next_chunk
        else:
            chunk += next_chunk
    if chunk:
        _publish_to_ws(session_id, {"type": "mode_text_chunk", "chunk": chunk})

    _publish_to_ws(session_id, {"type": "mode_text_stream_end"})


# ---------------------------------------------------------------------------
# Shared course-info loader with Redis cache (M-16)
# session_id → course mapping is immutable; cache for 1 hour.
# ---------------------------------------------------------------------------

_COURSE_CACHE_TTL = 3600  # seconds


async def _load_course(session_id: str) -> dict:
    """Fetch course metadata for a session, using Redis to avoid repeated DB calls.

    The session_id → course_id mapping never changes after session creation,
    so a 1-hour TTL is safe.
    """
    cache_key = f"course:session:{session_id}"
    try:
        cached = _get_redis().get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass  # Redis miss — fall through to DB

    course_id = await get_session_course_id(session_id)
    course = await get_course_info(course_id)

    try:
        _get_redis().setex(cache_key, _COURSE_CACHE_TTL, json.dumps(course, default=str))
    except Exception:
        pass  # Cache-write failure is non-fatal

    return course


# ---------------------------------------------------------------------------
# Learn mode Celery task
# ---------------------------------------------------------------------------

async def _retain_attachments(
    user_id: str, session_id: str, images: list | None
) -> list | None:
    """Archive submitted photos and return what to store on the conversation.

    Only the storage path is kept, never the base64 — a handful of those on one
    row would bloat the conversation table enormously and it is already in
    object storage. The client mints a signed URL when it needs to display one.

    Uploads run concurrently; one failure drops that image from the record
    rather than failing the turn, which has already been answered by this point.
    """
    if not images:
        return None

    results = await asyncio.gather(
        *[
            asyncio.to_thread(
                upload_student_image,
                user_id,
                session_id,
                base64.b64decode(img["data"]),
                img["media_type"],
            )
            for img in images
        ],
        return_exceptions=True,
    )

    stored = [
        {"bucket": STUDENT_UPLOADS_BUCKET, "path": path, "media_type": img["media_type"]}
        for img, path in zip(images, results)
        if isinstance(path, str) and path
    ]
    return stored or None


async def _do_rag_turn(
    session_id: str,
    user_id: str,
    message: str,
    mode: str,
    prefers_video: bool | None = None,  # M-17: passed from caller to skip duplicate DB fetch
    images: list | None = None,
):
    total_start = time.monotonic()

    memory, course = await asyncio.gather(load_memory(session_id), _load_course(session_id))

    if prefers_video is not None and mode != "review":
        response_format = "video" if prefers_video else "text"
    else:
        response_format = await response_format_for_mode(session_id, mode)

    # Feedback on a photograph is inherently visual — "your sign flips on line
    # three" only lands next to the working it refers to. An avatar narrating
    # it while the student stares at a talking head is the wrong medium, so an
    # image turn always answers on the board regardless of video preference.
    if images:
        response_format = "text"

    # Stream the text answer for BOTH text and video responses. In video mode
    # the persistent live avatar speaks in parallel, so streaming the transcript
    # lets the student start reading in ~2-3s instead of waiting the full
    # generate + TTS + avatar-render pipeline.
    should_stream_text = True

    def _on_progress(stage: str, label: str) -> None:
        # Drives the "what's happening" progress panel in the UI.
        _publish_to_ws(session_id, {"type": "status", "stage": stage, "label": label})

    if should_stream_text:
        _publish_to_ws(session_id, {"type": "text_stream_start"})

    def _on_chunk(text: str) -> None:
        _publish_to_ws(session_id, {"type": "text_chunk", "chunk": text})

    def _on_sources(sources: list) -> None:
        # Sent as soon as retrieval settles, ahead of the first token, so the
        # client can show what the answer is being drawn from while it writes.
        _publish_to_ws(session_id, {"type": "sources", "sources": sources})

    ai_start = time.monotonic()
    try:
        result = await query_rag(
            user_message=message,
            mode=mode,
            course_id=course["id"],
            course_persona=course.get("persona"),
            course_domain_topics=course.get("domain_topics"),
            memory=memory,
            on_chunk=_on_chunk if should_stream_text else None,
            on_progress=_on_progress,
            on_sources=_on_sources,
            # Video answers are spoken by a 90s-capped avatar → keep them short.
            concise=(response_format == "video"),
            # Learn-mode text answers are presented on the lesson board: slides
            # with typeset maths and plots, narrated in the client. The fences
            # ride the normal text stream, so the board builds as it arrives.
            board=(response_format != "video" and mode == "learn" and _board_enabled()),
            images=images,
            # Progressive scaffolding — a doctoral course should not be
            # taught like a first-year one (Proposal §2.2).
            academic_level=course.get("academic_level"),
        )
    except Exception as e:
        error_result = {"error": str(e), "status": "failed"}
        _publish_to_ws(session_id, error_result)
        return error_result
    ai_ms = int((time.monotonic() - ai_start) * 1000)

    tutor_text = result["response"]

    video_ms: int | None = None
    video_start = time.monotonic()
    if response_format == "video":
        _publish_to_ws(
            session_id,
            {"type": "status", "stage": "rendering_video", "label": "Creating your video"},
        )
    try:
        delivery = await maybe_generate_video(
            text=tutor_text,
            response_format=response_format,
            user_id=user_id,
            session_id=session_id,
            mode=mode,
        )
        if delivery.get("response_format") == "video":
            video_ms = int((time.monotonic() - video_start) * 1000)
    except Exception as e:
        logger.error("Video generation error  session=%s: %s", session_id, e, exc_info=True)
        delivery = {"response_format": "text", "video_url": None, "audio_url": None}

    total_ms = int((time.monotonic() - total_start) * 1000)

    await log_conversation(
        session_id=session_id,
        user_id=user_id,
        mode=mode,
        user_input=message,
        tutor_response=tutor_text,
        response_format=delivery.get("response_format", "text"),
        video_url=delivery.get("video_url"),
        audio_url=delivery.get("audio_url"),
        sources=result.get("sources"),
        attachments=await _retain_attachments(user_id, session_id, images),
    )

    analytics.log_request_metric(
        session_id=session_id,
        user_id=user_id,
        mode=mode,
        task_type=f"rag_turn_{mode}",
        response_format=delivery.get("response_format", "text"),
        total_processing_ms=total_ms,
        ai_processing_ms=ai_ms,
        video_generation_ms=video_ms,
    )

    # Research telemetry. Deliberately after log_conversation: the transcript
    # is the record that matters, the event is the measurement.
    events.emit(
        events.TURN_COMPLETED,
        user_id=user_id,
        course_id=course["id"],
        session_id=session_id,
        payload={
            "mode": mode,
            "response_format": delivery.get("response_format", "text"),
            "total_ms": total_ms,
            "ai_ms": ai_ms,
            "video_ms": video_ms,
            "sources": len(result.get("sources") or []),
            "had_images": bool(images),
        },
    )

    final_result = {
        "type": "response_complete",
        "mode": mode,
        "response": tutor_text,
        # Repeated on the terminal push so a client that reconnected mid-turn,
        # or missed the earlier `sources` event, can still render citations.
        "sources": result.get("sources", []),
        **delivery,
    }
    _publish_to_ws(session_id, final_result)
    return final_result


@shared_task
def process_rag_turn_task(
    session_id: str,
    user_id: str,
    message: str,
    mode: str = "learn",
    prefers_video: bool | None = None,  # M-17
    images: list | None = None,
):
    reset_async_supabase()
    try:
        result = asyncio.run(
            _do_rag_turn(session_id, user_id, message, mode, prefers_video, images)
        )
    except Exception as e:
        logger.exception("process_rag_turn_task failed  session=%s: %s", session_id, e)
        result = {"status": "failed", "error": "Processing failed. Please try again."}
        _publish_to_ws(session_id, result)

    return result


# ---------------------------------------------------------------------------
# Mode session START Celery task
# ---------------------------------------------------------------------------

async def _do_mode_session_start(
    session_id: str,
    user_id: str,
    mode: str,
    session_type: str,
    difficulty: str,
    mode_session_id: str,
):
    from app.ai.rag.modes_sessions.service import (
        generate_review_item,
        format_review_prompt,
        generate_application_scenario,
        format_application_prompt,
    )

    supabase = get_supabase()
    course = await _load_course(session_id)

    course_id = course["id"]
    course_name = course["name"]
    difficulty_descriptors = course.get("difficulty_descriptors") or {}

    ai_start = time.monotonic()

    if mode == "review":
        _publish_status(session_id, "preparing", "Preparing your first question")
        item, contexts = await generate_review_item(
            session_type=session_type,
            difficulty=difficulty,
            course_id=course_id,
            course_name=course_name,
            difficulty_descriptors=difficulty_descriptors,
        )
        prompt_text = format_review_prompt(item)
        await _stream_mode_text(
            session_id,
            mode="review",
            message_type="prompt",
            content=prompt_text,
            step=1,
            total_steps=10,
        )
        ai_ms = int((time.monotonic() - ai_start) * 1000)

        supabase.table("session_state").upsert(
            {
                "session_id": session_id,
                "mode_session_id": mode_session_id,
                "pending_mode": "review",
                "pending_payload": item,
                "step": 1,
                "total_steps": 1,
                "difficulty": difficulty,
                "contexts": contexts,
            }
        ).execute()

        await log_conversation(
            session_id=session_id,
            user_id=user_id,
            mode="review",
            user_input=f"(system) start review [{session_type}]",
            tutor_response=prompt_text,
            response_format="text",
            video_url=None,
            audio_url=None,
        )

        analytics.log_request_metric(
            session_id=session_id,
            user_id=user_id,
            mode="review",
            task_type="mode_session_start_review",
            response_format="text",
            total_processing_ms=ai_ms,
            ai_processing_ms=ai_ms,
        )

        return {
            "mode_session_id": mode_session_id,
            "mode": "review",
            "session_type": session_type,
            "difficulty": difficulty,
            "prompt": prompt_text,
            "response_format": "text",
            "video_url": None,
        }

    # APPLICATION
    _publish_status(session_id, "preparing", "Building your practice scenario")
    scenario, contexts = await generate_application_scenario(
        session_type=session_type,
        difficulty=difficulty,
        course_id=course_id,
        course_name=course_name,
        difficulty_descriptors=difficulty_descriptors,
    )
    prompt_text = format_application_prompt(scenario, step=1)
    await _stream_mode_text(
        session_id,
        mode="application",
        message_type="prompt",
        content=prompt_text,
        step=1,
        total_steps=3,
    )
    ai_ms = int((time.monotonic() - ai_start) * 1000)

    supabase.table("session_state").upsert(
        {
            "session_id": session_id,
            "mode_session_id": mode_session_id,
            "pending_mode": "application",
            "pending_payload": scenario,
            "step": 1,
            "total_steps": 3,
            "difficulty": difficulty,
            "contexts": contexts,
        }
    ).execute()

    await log_conversation(
        session_id=session_id,
        user_id=user_id,
        mode="application",
        user_input=f"(system) start application [{session_type}]",
        tutor_response=prompt_text,
        response_format="text",
        video_url=None,
        audio_url=None,
    )

    analytics.log_request_metric(
        session_id=session_id,
        user_id=user_id,
        mode="application",
        task_type="mode_session_start_application",
        response_format="text",
        total_processing_ms=ai_ms,
        ai_processing_ms=ai_ms,
    )

    return {
        "mode_session_id": mode_session_id,
        "mode": "application",
        "session_type": session_type,
        "difficulty": difficulty,
        "prompt": prompt_text,
        "response_format": "text",
        "video_url": None,
        "audio_url": None,
    }


@shared_task
def process_mode_session_start_task(
    session_id: str,
    user_id: str,
    mode: str,
    session_type: str,
    difficulty: str,
    mode_session_id: str,
):
    reset_async_supabase()
    try:
        result = asyncio.run(
            _do_mode_session_start(
                session_id, user_id, mode, session_type, difficulty, mode_session_id
            )
        )
    except Exception as e:
        logger.exception("process_mode_session_start_task failed  session=%s: %s", session_id, e)
        result = {"status": "failed", "error": "Processing failed. Please try again."}

    _publish_to_ws(session_id, result)
    return result


# ---------------------------------------------------------------------------
# Document ingestion Celery task
# Runs on the dedicated ingestion_tasks queue (-c 1) so spaCy / unstructured
# memory spikes are fully isolated from user-facing text and video workers.
# ---------------------------------------------------------------------------

@shared_task
def process_ingestion_task(
    document_id: str,
    file_content_b64: str,   # base64-encoded file bytes (JSON-serialisable)
    filename: str,
    course_id: str,
    doc_type: str,
    default_mode: str,
    difficulty: str,
):
    import base64
    from app.ai.ingestion.service import process_document_background

    file_content = base64.b64decode(file_content_b64)
    reset_async_supabase()
    try:
        asyncio.run(
            process_document_background(
                document_id=document_id,
                file_content=file_content,
                filename=filename,
                course_id=course_id,
                doc_type=doc_type,
                default_mode=default_mode,
                difficulty=difficulty,
            )
        )
    except Exception as e:
        logger.exception(
            "process_ingestion_task failed  doc=%s: %s", document_id, e
        )
    return {"document_id": document_id, "status": "complete"}


# Render tasks deliberately live in app/media/render/tasks.py, not here — see
# the CELERY_INCLUDE note in app/core/celery_app.py. The API dispatches them by
# name so it never imports manim.


# ---------------------------------------------------------------------------
# Mode session TURN Celery task
# ---------------------------------------------------------------------------

async def _do_mode_session_turn(
    mode_session_id: str,
    session_id: str,
    user_id: str,
    mode: str,
    session_type: str,
    student_answer: str,
    pending_payload: dict,
    contexts: list,
    step: int,
    total_steps: int,
    difficulty: str,
    current_item: int,
    total_items: int,
):
    from datetime import datetime, timezone
    from app.ai.rag.modes_sessions.service import (
        evaluate_review_answer,
        format_review_prompt,
        generate_review_item,
        adjust_difficulty,
        evaluate_application_answer,
        format_application_prompt,
    )

    supabase = get_supabase()
    course = await _load_course(session_id)

    course_id = course["id"]
    course_name = course["name"]
    difficulty_descriptors = course.get("difficulty_descriptors") or {}

    def _now_iso():
        return datetime.now(timezone.utc).isoformat()

    ai_start = time.monotonic()

    # ------------------------------------------------------------------ #
    # APPLICATION                                                          #
    # ------------------------------------------------------------------ #
    if mode == "application":
        _publish_status(session_id, "evaluating", "Evaluating your answer")
        eval_out = await evaluate_application_answer(
            scenario=pending_payload,
            step=step,
            student_answer=student_answer,
            contexts=contexts,
            course_name=course_name,
        )
        ai_ms = int((time.monotonic() - ai_start) * 1000)

        feedback_text = (
            f"Verdict: {eval_out['verdict']}\n"
            f"Score: {eval_out['score']}\n"
            f"Feedback: {eval_out['feedback']}"
        )
        await _stream_mode_text(
            session_id,
            mode="application",
            message_type="evaluation",
            content=eval_out["feedback"],
            step=step,
            total_steps=total_steps,
        )

        await log_conversation(
            session_id=session_id,
            user_id=user_id,
            mode="application",
            user_input=student_answer,
            tutor_response=feedback_text,
            response_format="text",
            video_url=None,
            audio_url=None,
        )

        analytics.log_request_metric(
            session_id=session_id,
            user_id=user_id,
            mode="application",
            task_type="mode_session_turn_application",
            response_format="text",
            total_processing_ms=ai_ms,
            ai_processing_ms=ai_ms,
        )

        supabase.table("mode_sessions").update(
            {"message_count": current_item}
        ).eq("id", mode_session_id).execute()

        if step < total_steps:
            next_step = step + 1
            next_prompt = format_application_prompt(pending_payload, next_step)
            await _stream_mode_text(
                session_id,
                mode="application",
                message_type="prompt",
                content=next_prompt,
                step=next_step,
                total_steps=total_steps,
            )

            supabase.table("session_state").update({"step": next_step}).eq(
                "mode_session_id", mode_session_id
            ).execute()
            supabase.table("mode_sessions").update({"current_item": next_step}).eq(
                "id", mode_session_id
            ).execute()

            await log_conversation(
                session_id=session_id,
                user_id=user_id,
                mode="application",
                user_input="(system) next guided question",
                tutor_response=next_prompt,
                response_format="text",
                video_url=None,
                audio_url=None,
            )

            return {
                "mode": "application",
                "type": "evaluation",
                "evaluation": eval_out,
                "next_prompt": next_prompt,
                "response_format": "text",
            }

        key_points = pending_payload.get("key_learning_points", [])
        key_text = (
            "Key Learning Points: " + " | ".join(key_points)
            if key_points
            else "Application scenario completed."
        )
        await _stream_mode_text(
            session_id,
            mode="application",
            message_type="completed",
            content=key_text,
            step=total_steps,
            total_steps=total_steps,
        )

        await log_conversation(
            session_id=session_id,
            user_id=user_id,
            mode="application",
            user_input="(system) scenario complete",
            tutor_response=key_text,
            response_format="text",
            video_url=None,
            audio_url=None,
        )

        supabase.table("mode_sessions").update(
            {"completed": True, "ended_at": _now_iso()}
        ).eq("id", mode_session_id).execute()

        supabase.table("session_state").delete().eq(
            "mode_session_id", mode_session_id
        ).execute()

        analytics.close_mode_session(mode_session_id)

        return {
            "mode": "application",
            "type": "completed",
            "evaluation": eval_out,
            "key_learning_points": key_points,
            "summary": key_text,
            "response_format": "text",
        }

    # ------------------------------------------------------------------ #
    # REVIEW                                                               #
    # ------------------------------------------------------------------ #
    _publish_status(session_id, "evaluating", "Evaluating your answer")
    eval_out = await evaluate_review_answer(
        item=pending_payload,
        student_answer=student_answer,
        contexts=contexts,
        course_name=course_name,
    )
    ai_ms = int((time.monotonic() - ai_start) * 1000)

    score = float(eval_out.get("score", 0.0))
    next_difficulty = adjust_difficulty(difficulty, score)

    feedback_text = (
        f"Verdict: {eval_out['verdict']}\n"
        f"Score: {eval_out['score']}\n"
        f"Rubric: {eval_out.get('rubric_level', '')}\n"
        f"Feedback: {eval_out['feedback']}"
    )
    await _stream_mode_text(
        session_id,
        mode="review",
        message_type="evaluation",
        content=eval_out["feedback"],
        step=current_item,
        total_steps=total_items,
    )

    await log_conversation(
        session_id=session_id,
        user_id=user_id,
        mode="review",
        user_input=student_answer,
        tutor_response=feedback_text,
        response_format="text",
        video_url=None,
        audio_url=None,
    )

    analytics.log_request_metric(
        session_id=session_id,
        user_id=user_id,
        mode="review",
        task_type="mode_session_turn_review",
        response_format="text",
        total_processing_ms=ai_ms,
        ai_processing_ms=ai_ms,
    )

    supabase.table("mode_sessions").update(
        {"message_count": current_item}
    ).eq("id", mode_session_id).execute()

    if current_item >= total_items:
        supabase.table("mode_sessions").update(
            {"completed": True, "ended_at": _now_iso()}
        ).eq("id", mode_session_id).execute()

        supabase.table("session_state").delete().eq(
            "mode_session_id", mode_session_id
        ).execute()

        analytics.close_mode_session(mode_session_id)

        return {
            "mode": "review",
            "type": "completed",
            "evaluation": eval_out,
            "response_format": "text",
            "video_url": None,
        }

    _publish_status(session_id, "preparing", "Preparing the next question")
    item2, ctx2 = await generate_review_item(
        session_type=session_type,
        difficulty=next_difficulty,
        course_id=course_id,
        course_name=course_name,
        difficulty_descriptors=difficulty_descriptors,
    )
    next_prompt = format_review_prompt(item2)
    await _stream_mode_text(
        session_id,
        mode="review",
        message_type="prompt",
        content=next_prompt,
        step=current_item + 1,
        total_steps=total_items,
    )

    supabase.table("session_state").upsert(
        {
            "session_id": session_id,
            "mode_session_id": mode_session_id,
            "pending_mode": "review",
            "pending_payload": item2,
            "step": 1,
            "total_steps": 1,
            "difficulty": next_difficulty,
            "contexts": ctx2,
        }
    ).execute()

    supabase.table("mode_sessions").update(
        {"difficulty": next_difficulty, "current_item": current_item + 1}
    ).eq("id", mode_session_id).execute()

    await log_conversation(
        session_id=session_id,
        user_id=user_id,
        mode="review",
        user_input="(system) next question",
        tutor_response=next_prompt,
        response_format="text",
        video_url=None,
        audio_url=None,
    )

    return {
        "mode": "review",
        "type": "evaluation",
        "evaluation": eval_out,
        "next_prompt": next_prompt,
        "next_difficulty": next_difficulty,
        "response_format": "text",
        "video_url": None,
    }


@shared_task
def process_mode_session_turn_task(
    mode_session_id: str,
    session_id: str,
    user_id: str,
    mode: str,
    session_type: str,
    student_answer: str,
    pending_payload: dict,
    contexts: list,
    step: int,
    total_steps: int,
    difficulty: str,
    current_item: int,
    total_items: int,
):
    reset_async_supabase()
    try:
        result = asyncio.run(
            _do_mode_session_turn(
                mode_session_id,
                session_id,
                user_id,
                mode,
                session_type,
                student_answer,
                pending_payload,
                contexts,
                step,
                total_steps,
                difficulty,
                current_item,
                total_items,
            )
        )
    except Exception as e:
        logger.exception(
            "process_mode_session_turn_task failed  mode_session=%s: %s", mode_session_id, e
        )
        result = {"status": "failed", "error": "Processing failed. Please try again."}

    _publish_to_ws(session_id, result)
    return result
