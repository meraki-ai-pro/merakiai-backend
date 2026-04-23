import asyncio
import hashlib
import logging

from app.db.supabase import get_supabase
from app.ai.ingestion.parser import parse_document
from app.ai.ingestion.chunker import chunk_text
from app.ai.ingestion.embedder import embed_chunks
from app.ai.ingestion.pinecone import upsert_vectors
from app.ai.ingestion.normalizer import normalize_sections

logger = logging.getLogger(__name__)


def _resolve_default_mode(default_mode: str | None, doc_type: str) -> str:
    mode_map = {"knowledge": "learn", "assessment": "review", "practice": "application"}
    allowed = {"learn", "review", "application"}

    if not default_mode or default_mode == "default":
        resolved = mode_map.get(doc_type)
        if not resolved:
            raise ValueError(f"Unsupported doc_type: {doc_type}")
        return resolved

    if default_mode not in allowed:
        raise ValueError(f"Unsupported default_mode: {default_mode}")

    return default_mode


def _resolve_difficulty(difficulty: str | None) -> str:
    allowed = {"beginner", "intermediate", "advanced"}
    synonyms = {
        "easy": "beginner",
        "medium": "intermediate",
        "normal": "intermediate",
        "hard": "advanced",
    }

    if not difficulty:
        return "beginner"

    normalized = difficulty.strip().lower()
    normalized = synonyms.get(normalized, normalized)

    if normalized not in allowed:
        raise ValueError(f"Unsupported difficulty: {difficulty}")

    return normalized


def _resolve_doc_type(doc_type: str | None) -> str:
    allowed = {"knowledge", "assessment", "practice"}
    synonyms = {"learn": "knowledge", "review": "assessment", "application": "practice"}

    if not doc_type:
        raise ValueError("doc_type is required")

    normalized = doc_type.strip().lower()
    normalized = synonyms.get(normalized, normalized)

    if normalized not in allowed:
        raise ValueError(f"Unsupported doc_type: {doc_type}")

    return normalized


# Removed redundant imports and logger from here


async def ingest_document(
    file, course_id, doc_type, default_mode, difficulty, version, user_id, background_tasks
):
    doc_type = _resolve_doc_type(doc_type)
    default_mode = _resolve_default_mode(default_mode, doc_type)
    difficulty = _resolve_difficulty(difficulty)

    supabase = get_supabase()

    # 1. Register document (FAST)
    doc_response = (
        supabase.table("documents")
        .insert(
            {
                "title": file.filename,
                "source_filename": file.filename,
                "course_id": course_id,
                "doc_type": doc_type,
                "default_mode": default_mode,
                "difficulty": difficulty,
                "version": version,
                "created_by": user_id,
                "status": "processing",
            }
        )
        .execute()
    )

    if not doc_response.data:
        raise RuntimeError("Failed to register document in database.")

    doc = doc_response.data[0]
    document_id = doc["id"]

    # Read file content now because the stream might close after response
    file_content = await file.read()
    filename = file.filename

    # Schedule background processing
    background_tasks.add_task(
        process_document_background,
        document_id=document_id,
        file_content=file_content,
        filename=filename,
        course_id=course_id,
        doc_type=doc_type,
        default_mode=default_mode,
        difficulty=difficulty,
    )

    return {
        "document_id": document_id,
        "status": "processing",
        "message": "Document ingestion started in background.",
    }


async def process_document_background(
    document_id: str,
    file_content: bytes,
    filename: str,
    course_id: str,
    doc_type: str,
    default_mode: str,
    difficulty: str,
):
    """
    Heavy lifting: Parse, Chunk, Embed, Index.
    Runs in background to keep API responsive.
    """
    supabase = get_supabase()

    class FileWrapper:
        def __init__(self, content, name):
            self.content = content
            self.filename = name
            self.file = self  # Mocking .file.read() if needed

        def read(self):
            return self.content

    try:
        file_obj = FileWrapper(file_content, filename)

        # 2. Parse (CPU bound)
        sections = await asyncio.to_thread(parse_document, file_obj)

        # 3. Normalize (CPU bound)
        normalised_sections = await asyncio.to_thread(normalize_sections, sections)

        # 4. Chunk (CPU bound)
        chunks = await asyncio.to_thread(chunk_text, normalised_sections, doc_type)

        if not chunks:
            supabase.table("documents").update({"status": "no_content"}).eq(
                "id", document_id
            ).execute()
            return

        # 5. Embed (Network I/O)
        embeddings = await embed_chunks([c["text"] for c in chunks])

        # 6. Upsert to Pinecone  (namespace = "{course_id}-{mode}")
        namespace = f"{course_id}-{default_mode}"
        pinecone_ids = await asyncio.to_thread(
            upsert_vectors, embeddings, chunks, namespace=namespace
        )

        # 7. Batch Store metadata
        batch_metadata = []
        for chunk, pid in zip(chunks, pinecone_ids):
            content_hash = hashlib.sha256(chunk["text"].encode()).hexdigest()
            batch_metadata.append(
                {
                    "document_id": document_id,
                    "pinecone_id": pid,
                    "pinecone_namespace": namespace,
                    "mode": chunk["mode"],
                    "topic": chunk.get("topic"),
                    "difficulty": difficulty,
                    "content_hash": content_hash,
                }
            )

        if batch_metadata:
            supabase.table("document_chunks").insert(batch_metadata).execute()

        # 8. Mark Ready
        supabase.table("documents").update(
            {"status": "ready", "total_chunks": len(chunks)}
        ).eq("id", document_id).execute()

    except Exception as e:
        logger.error(f"Background ingestion failed for {document_id}: {str(e)}")
        supabase.table("documents").update({"status": "failed"}).eq(
            "id", document_id
        ).execute()
