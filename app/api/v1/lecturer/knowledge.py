"""Knowledge files: upload, tag by mode, stage, test, publish, delete.

The workflow this exists to support (Lecturer doc §4.2): upload as a draft, run
a sample student question against it, and only then publish. Without the draft
state a lecturer's first upload is live to students before they have read a
single generated answer.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.ai.ingestion.service import (
    ingest_document,
    parse_question_formats,
    parse_target_modes,
    register_document,
)
from app.ai.ingestion.video import VIDEO_CONTENT_TYPES
from app.ai.rag.visibility import invalidate as invalidate_visibility
from app.core import audit
from app.core.auth import assert_course_owner, lecturer_guard
from app.db.supabase import get_supabase
from app.media.storage_service import COURSE_VIDEOS_BUCKET, _safe_segment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/courses/{course_id}/knowledge", tags=["Instructor – Knowledge"])

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# Leading bytes each accepted format must actually start with. The extension
# is caller-supplied; the bytes are not. OOXML formats (.docx, .pptx) are ZIP
# containers, hence the shared signature.
_MAGIC = {
    "pdf": b"%PDF",
    "docx": b"PK\x03\x04",
    "doc": b"PK\x03\x04",
    "pptx": b"PK\x03\x04",
}

# One source of truth. These were two literals that had to agree, and when
# they drifted the mismatch surfaced as a KeyError -> 500 on upload instead
# of a clean 415: the allowlist admitted an extension _MAGIC had never heard
# of. Deriving one from the other makes that state unrepresentable.
_ALLOWED_EXTENSIONS = frozenset(_MAGIC)


class KnowledgeUpdate(BaseModel):
    is_published: bool | None = None
    topic: str | None = None
    title: str | None = Field(None, max_length=300)
    # Retagging without re-uploading. Re-ingesting a 50 MB PDF to change one
    # dropdown would cost a re-embed of every chunk for no benefit — the tags
    # live on the document row and are read at query time.
    difficulty: Literal["basic", "intermediate", "advanced"] | None = None
    question_formats: list[Literal["mcq", "fill_blank", "short_answer"]] | None = None


class VideoAdd(BaseModel):
    """A lecture video: ``filename`` to upload one, or ``link`` to a hosted one."""
    filename: str | None = Field(None, max_length=300)
    link: str | None = Field(None, max_length=2000, pattern=r"^https://")
    title: str | None = Field(None, max_length=300)
    default_mode: str = "learn"
    target_modes: list[str] | None = None
    difficulty: str = "basic"
    topic: str | None = None
    is_published: bool = False


class TestQuery(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)
    mode: str = "learn"


_LIST_COLUMNS = (
    "id, title, source_filename, doc_type, default_mode, target_modes, "
    "is_published, topic, difficulty, status, total_chunks, version, "
    "storage_path, media_url, created_at"
)


@router.get("")
def list_knowledge(course_id: str, user=Depends(lecturer_guard)):
    assert_course_owner(user, course_id)
    sb = get_supabase()

    def _fetch(columns: str):
        return (
            sb.table("documents").select(columns)
            .eq("course_id", course_id).order("created_at", desc=True)
            .execute().data or []
        )

    try:
        rows = _fetch(f"{_LIST_COLUMNS}, question_formats")
    except Exception:  # noqa: BLE001 — sql/013 may not be applied yet
        rows = _fetch(_LIST_COLUMNS)

    return {"documents": [r for r in rows if not r.get("deleted_at")]}


@router.post("")
async def upload_knowledge(
    course_id: str,
    file: UploadFile,
    request: Request,
    doc_type: str = "knowledge",
    default_mode: str = "learn",
    # Matches documents_difficulty_check (basic|intermediate|advanced);
    # _resolve_difficulty still accepts 'beginner' as a synonym for callers
    # that send it.
    difficulty: str = "basic",
    version: str = "1",
    target_modes: str | None = None,
    topic: str | None = None,
    # Review material only: which of mcq / fill_blank / short_answer this file
    # can be turned into. Comma-separated; omitted means "any".
    question_formats: str | None = None,
    is_published: bool = False,
    user=Depends(lecturer_guard),
):
    """Upload a knowledge file.

    ``is_published`` defaults to **false** here, unlike the admin route. A
    lecturer's intended flow is upload → test-query → publish, and defaulting
    to live would skip the review step the draft state exists for.

    ``difficulty`` and ``question_formats`` are the two tags that steer
    generation. Review files carry both; scenario (application) files carry
    difficulty alone, because a scenario has no question format to pick. Both
    are preferences at retrieval time, not filters — see
    app/ai/rag/visibility.py for why a hard filter would empty a course's
    knowledge base the first time a lecturer tagged one file.
    """
    assert_course_owner(user, course_id)

    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
        )

    contents = await file.read()
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the 50 MB limit ({len(contents) // (1024*1024)} MB received).",
        )
    if not contents.startswith(_MAGIC[ext]):
        # The extension is caller-supplied; the bytes are not.
        raise HTTPException(
            status_code=415, detail=f"File does not appear to be a valid {ext.upper()}."
        )
    await file.seek(0)

    try:
        modes = parse_target_modes(target_modes, default_mode)
        formats = parse_question_formats(question_formats)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Rejected rather than silently dropped: a lecturer who ticks MCQ on a
    # Learn-only file has misunderstood what the tag does, and storing it
    # would leave them believing Review questions will come from this file.
    if formats and "review" not in (modes or [default_mode]):
        raise HTTPException(
            status_code=400,
            detail=(
                "Question formats apply to Review material. Tick Review as one "
                "of this file's modes, or leave the formats unset."
            ),
        )

    result = await ingest_document(
        file=file,
        course_id=course_id,
        doc_type=doc_type,
        default_mode=default_mode,
        difficulty=difficulty,
        version=version,
        user_id=user["id"],
        target_modes=modes,
        is_published=is_published,
        topic=topic,
        question_formats=formats,
    )

    audit.record(
        actor=user, action="knowledge.upload", resource_type="document",
        resource_id=result.get("document_id"), course_id=course_id,
        new_values={
            "filename": file.filename, "target_modes": modes,
            "is_published": is_published, "topic": topic,
            "difficulty": difficulty, "question_formats": formats,
        },
        request=request,
    )
    return result


@router.post("/videos")
def add_video(course_id: str, payload: VideoAdd, request: Request, user=Depends(lecturer_guard)):
    """Register a lecture video as course material.

    A link is transcribed straight away. An upload returns a signed upload URL:
    the browser sends the file directly to storage (no 50 MB API cap, nothing
    large through this process), then calls ``/{document_id}/video-uploaded``.
    """
    assert_course_owner(user, course_id)
    if bool(payload.filename) == bool(payload.link):
        raise HTTPException(status_code=400, detail="Send a filename to upload, or a link — one of the two.")
    if payload.filename:
        ext = payload.filename.rsplit(".", 1)[-1].lower()
        if ext not in VIDEO_CONTENT_TYPES:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported video type '.{ext}'. Allowed: {', '.join(sorted(VIDEO_CONTENT_TYPES))}",
            )
    if payload.link:
        # The same link twice indexes the same lecture twice, and deleting one
        # copy then looks like a delete that did not work: the other copy is
        # still cited and still listed for students.
        live = (
            get_supabase().table("documents").select("id, deleted_at")
            .eq("course_id", course_id).eq("media_url", payload.link).execute().data or []
        )
        if any(not d.get("deleted_at") for d in live):
            raise HTTPException(status_code=409, detail="That video link is already on this course.")
    try:
        modes = parse_target_modes(",".join(payload.target_modes or []), payload.default_mode)
        doc = register_document(
            payload.title or payload.filename or payload.link, course_id, "knowledge",
            payload.default_mode, payload.difficulty, "1", user["id"],
            target_modes=modes, is_published=payload.is_published, topic=payload.topic,
            extra={"media_url": payload.link} if payload.link else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    audit.record(
        actor=user, action="knowledge.video", resource_type="document",
        resource_id=doc["id"], course_id=course_id,
        new_values={"link": payload.link, "filename": payload.filename, "target_modes": modes},
        request=request,
    )
    if payload.link:
        _queue_video(doc)
        return {"document_id": doc["id"], "status": "processing"}

    path = f"{_safe_segment(course_id)}/{_safe_segment(doc['id'])}/{_safe_segment(payload.filename)}"
    sb = get_supabase()
    sb.table("documents").update({"storage_path": path}).eq("id", doc["id"]).execute()
    upload = sb.storage.from_(COURSE_VIDEOS_BUCKET).create_signed_upload_url(path)
    return {
        "document_id": doc["id"], "status": "awaiting_upload",
        "bucket": COURSE_VIDEOS_BUCKET, "path": path, "token": upload["token"],
    }


@router.post("/{document_id}/video-uploaded")
def video_uploaded(course_id: str, document_id: str, user=Depends(lecturer_guard)):
    """The browser finished uploading: start transcription."""
    assert_course_owner(user, course_id)
    rows = (
        get_supabase().table("documents")
        .select("id, source_filename, course_id, default_mode, difficulty, storage_path")
        .eq("id", document_id).eq("course_id", course_id).execute().data
    )
    if not rows or not rows[0].get("storage_path"):
        raise HTTPException(status_code=404, detail="No uploaded video with that id on this course")
    _queue_video(rows[0])
    return {"document_id": document_id, "status": "processing"}


def _queue_video(doc: dict) -> None:
    from app.ai.tasks import process_video_ingestion_task

    try:
        process_video_ingestion_task.apply_async(
            args=[doc["id"], doc["source_filename"], doc["course_id"], doc["default_mode"], doc["difficulty"]],
            queue="ingestion_tasks",
            routing_key="ingestion",
        )
    except Exception as exc:  # noqa: BLE001 — broker availability is external
        logger.exception("Could not queue video ingestion for %s", doc["id"])
        get_supabase().table("documents").update({"status": "failed"}).eq(
            "id", doc["id"]
        ).execute()
        raise HTTPException(
            status_code=503,
            detail="The video was saved, but transcription could not be queued. Retry it shortly.",
        ) from exc


@router.post("/{document_id}/retry-video")
def retry_video(course_id: str, document_id: str, user=Depends(lecturer_guard)):
    """Requeue a failed or stale lecture-video transcription.

    Jobs can be lost when the broker or ingestion worker is unavailable during
    a deployment. Keeping an explicit retry makes that recoverable without
    deleting and re-adding a large upload or hosted link.
    """
    assert_course_owner(user, course_id)
    sb = get_supabase()
    rows = (
        sb.table("documents")
        .select(
            "id, source_filename, course_id, default_mode, difficulty, "
            "storage_path, media_url"
        )
        .eq("id", document_id)
        .eq("course_id", course_id)
        .execute()
        .data
        or []
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Document not found on this course")

    doc = rows[0]
    if not doc.get("media_url") and not doc.get("storage_path"):
        raise HTTPException(status_code=409, detail="This document has no video to transcribe")

    sb.table("documents").update({"status": "processing", "total_chunks": 0}).eq(
        "id", document_id
    ).execute()
    _queue_video(doc)
    return {"document_id": document_id, "status": "processing"}


@router.patch("/{document_id}")
def update_knowledge(
    course_id: str,
    document_id: str,
    payload: KnowledgeUpdate,
    request: Request,
    user=Depends(lecturer_guard),
):
    """Publish, unpublish, retitle, retopic or retag a file."""
    assert_course_owner(user, course_id)

    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    sb = get_supabase()
    before = (
        sb.table("documents").select("id, is_published, title, topic, difficulty")
        .eq("id", document_id).eq("course_id", course_id).execute().data
    )
    if not before:
        raise HTTPException(status_code=404, detail="Document not found on this course")

    try:
        sb.table("documents").update(updates).eq("id", document_id).execute()
    except Exception as exc:  # noqa: BLE001 — sql/013 may not be applied yet
        if "question_formats" not in updates:
            raise
        logger.warning(
            "question_formats update rejected; applying the rest. "
            "Apply sql/013_roster_import_narration_and_upload_tags.sql: %s", exc,
        )
        updates.pop("question_formats")
        if updates:
            sb.table("documents").update(updates).eq("id", document_id).execute()

    # The retriever caches visibility for 15s. A lecturer who unpublishes a file
    # with a wrong formula in it expects the next answer to be clean, not the
    # one after that.
    invalidate_visibility(course_id)

    audit.record(
        actor=user, action="knowledge.update", resource_type="document",
        resource_id=document_id, course_id=course_id,
        old_values=before[0], new_values=updates, request=request,
    )
    return {"status": "ok", "document_id": document_id, **updates}


@router.delete("/{document_id}")
def delete_knowledge(
    course_id: str, document_id: str, request: Request, user=Depends(lecturer_guard)
):
    """Soft-delete: hide from retrieval now, purge vectors separately.

    Deliberately not a hard delete. The vectors live in Pinecone and the file in
    storage; tearing all three down synchronously would leave a half-deleted
    document if any step failed. Setting deleted_at removes it from every answer
    immediately, which is the part that matters.
    """
    assert_course_owner(user, course_id)

    sb = get_supabase()
    before = (
        sb.table("documents").select("id, title, is_published, media_url, storage_path, source_filename")
        .eq("id", document_id).eq("course_id", course_id).execute().data
    )
    if not before:
        raise HTTPException(status_code=404, detail="Document not found on this course")
    doc = before[0]
    is_video = bool(doc.get("media_url")) or (
        (doc.get("source_filename") or "").rsplit(".", 1)[-1].lower() in VIDEO_CONTENT_TYPES
    )

    update = {"deleted_at": "now()", "is_published": False}
    if is_video:
        # A deleted video must not survive anywhere: the link is cleared so no
        # watch URL can be minted from it.
        update["media_url"] = None
    sb.table("documents").update(update).eq("id", document_id).execute()

    invalidate_visibility(course_id)
    if is_video:
        _purge_video(sb, doc)

    audit.record(
        actor=user, action="knowledge.delete", resource_type="document",
        resource_id=document_id, course_id=course_id,
        old_values=before[0], request=request,
    )
    return {"status": "ok", "document_id": document_id, "deleted": True}


def _purge_video(sb, doc: dict) -> None:
    """Remove a deleted video's transcript vectors and uploaded file.

    Best-effort and after the soft delete, which already hides it everywhere: a
    Pinecone or storage hiccup leaves an orphan to clean later, never a live video.
    """
    from collections import defaultdict

    from app.ai.ingestion.pinecone import delete_vectors

    try:
        chunks = sb.table("document_chunks").select("pinecone_id, pinecone_namespace").eq(
            "document_id", doc["id"]
        ).execute().data or []
        by_namespace: dict[str, list[str]] = defaultdict(list)
        for c in chunks:
            by_namespace[c["pinecone_namespace"]].append(c["pinecone_id"])
        for namespace, ids in by_namespace.items():
            delete_vectors(ids, namespace)
        sb.table("document_chunks").delete().eq("document_id", doc["id"]).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not purge vectors for deleted video %s: %s", doc["id"], exc)
    if doc.get("storage_path") and not doc.get("media_url"):
        try:
            sb.storage.from_(COURSE_VIDEOS_BUCKET).remove([doc["storage_path"]])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not remove uploaded video %s: %s", doc["storage_path"], exc)


@router.post("/test-query")
async def test_query(
    course_id: str, payload: TestQuery, user=Depends(lecturer_guard)
):
    """Run a sample student question against this course's material.

    Retrieval only — no generated answer. What a lecturer needs to see before
    publishing is *which passages come back*, and a fluent answer over bad
    retrieval is exactly the thing that hides a problem.
    """
    assert_course_owner(user, course_id)

    from app.ai.rag.retriever import retrieve

    chunks = await retrieve(query=payload.question, mode=payload.mode, course_id=course_id)
    return {
        "question": payload.question,
        "mode": payload.mode,
        "results": [
            {
                "text": c.text[:600],
                "source_filename": c.source_filename,
                "section_title": c.section_title,
                "page": c.page,
                "score": round(c.dense_score, 4),
                "relevance_band": c.relevance_band,
            }
            for c in chunks
        ],
        "count": len(chunks),
    }
