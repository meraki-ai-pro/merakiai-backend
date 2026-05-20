from __future__ import annotations

import asyncio
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.auth import admin_guard
from app.db.supabase import get_supabase

router = APIRouter(prefix="/documents", tags=["Admin – Documents"])


class DocumentUpdatePayload(BaseModel):
    title: str | None = None
    difficulty: str | None = None
    version: str | None = None
    doc_type: str | None = None
    default_mode: str | None = None


@router.get("")
def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = None,
    course_id: str | None = None,
    doc_type: str | None = None,
    _user=Depends(admin_guard),
):
    sb = get_supabase()
    offset = (page - 1) * page_size

    q = sb.table("documents").select(
        "id, title, source_filename, doc_type, default_mode, difficulty, version, status, total_chunks, course_id, created_at, updated_at",
        count="exact",
    )
    if status:
        q = q.eq("status", status)
    if course_id:
        q = q.eq("course_id", course_id)
    if doc_type:
        q = q.eq("doc_type", doc_type)

    res = q.order("created_at", desc=True).range(offset, offset + page_size - 1).execute()
    return {
        "documents": res.data or [],
        "total": res.count or 0,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{doc_id}")
def get_document(doc_id: str, _user=Depends(admin_guard)):
    sb = get_supabase()

    doc = sb.table("documents").select("*").eq("id", doc_id).single().execute().data
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # M-20: get exact count cheaply, then fetch only the rows we actually display.
    count_res = (
        sb.table("document_chunks")
        .select("id", count="exact")
        .eq("document_id", doc_id)
        .limit(0)
        .execute()
    )
    chunk_count = count_res.count or 0

    # Fetch enough rows to cover the preview (10) and collect all distinct
    # namespaces (documents rarely span more than a handful of namespaces).
    chunks = (
        sb.table("document_chunks")
        .select("id, pinecone_id, pinecone_namespace, mode, topic, difficulty, created_at")
        .eq("document_id", doc_id)
        .limit(200)
        .execute()
        .data or []
    )

    namespaces = list({c["pinecone_namespace"] for c in chunks})

    return {
        "document": doc,
        "chunk_count": chunk_count,
        "namespaces": namespaces,
        "chunks_preview": chunks[:10],
    }


@router.patch("/{doc_id}")
def update_document(doc_id: str, payload: DocumentUpdatePayload, _user=Depends(admin_guard)):
    sb = get_supabase()

    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    _VALID_DIFFICULTY = {"basic", "intermediate", "advanced"}
    _VALID_MODE = {"learn", "review", "application"}
    _VALID_DOC_TYPE = {"knowledge", "assessment", "practice"}

    if "difficulty" in updates and updates["difficulty"] not in _VALID_DIFFICULTY:
        raise HTTPException(status_code=400, detail=f"difficulty must be one of {_VALID_DIFFICULTY}")
    if "default_mode" in updates and updates["default_mode"] not in _VALID_MODE:
        raise HTTPException(status_code=400, detail=f"default_mode must be one of {_VALID_MODE}")
    if "doc_type" in updates and updates["doc_type"] not in _VALID_DOC_TYPE:
        raise HTTPException(status_code=400, detail=f"doc_type must be one of {_VALID_DOC_TYPE}")

    updates["updated_at"] = "now()"
    res = sb.table("documents").update(updates).eq("id", doc_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"status": "ok", "document": res.data[0]}


@router.delete("/{doc_id}")
async def delete_document(doc_id: str, _user=Depends(admin_guard)):
    """Delete document, its chunk metadata, and its Pinecone vectors."""
    sb = get_supabase()

    doc = sb.table("documents").select("id, title").eq("id", doc_id).single().execute().data
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Gather all chunk IDs and namespaces before deleting
    chunks = sb.table("document_chunks").select(
        "pinecone_id, pinecone_namespace"
    ).eq("document_id", doc_id).execute().data or []

    by_namespace: dict[str, list[str]] = defaultdict(list)
    for c in chunks:
        by_namespace[c["pinecone_namespace"]].append(c["pinecone_id"])

    # Delete from Pinecone (non-blocking, errors are logged not raised)
    if by_namespace:
        from app.ai.ingestion.pinecone import delete_vectors
        for ns, ids in by_namespace.items():
            try:
                await asyncio.to_thread(delete_vectors, ids, ns)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    "Pinecone delete failed doc=%s ns=%s err=%s", doc_id, ns, exc
                )

    # Delete chunks then document (FK constraint)
    sb.table("document_chunks").delete().eq("document_id", doc_id).execute()
    sb.table("documents").delete().eq("id", doc_id).execute()

    return {
        "status": "ok",
        "deleted_document": doc_id,
        "chunks_removed": len(chunks),
        "namespaces_cleared": list(by_namespace.keys()),
    }
