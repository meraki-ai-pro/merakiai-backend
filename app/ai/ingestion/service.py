import asyncio
import base64
import hashlib
import logging

from app.db.supabase import get_supabase
from app.ai.ingestion.embedder import embed_chunks
from app.ai.ingestion.math_ocr import apply_math_ocr
from app.ai.ingestion.math_parser import parse_blocks
from app.ai.ingestion.namespaces import namespace_for
from app.ai.ingestion.pinecone import upsert_chunks
from app.ai.ingestion.structured_chunker import build_chunks
from app.media.storage_service import upload_course_document

logger = logging.getLogger(__name__)

# The mode vocabulary the whole application uses. document_chunks.mode also
# accepts the legacy 'practice' — see 001_allow_application_mode_in_document_chunks.sql
_VALID_MODES = ("learn", "review", "application")

# What a Review-mode file can be turned into. 'flashcard' is deliberately
# absent — the client removed it from the student's question-format picker, so
# tagging material for it would promise a format nothing can generate.
_VALID_QUESTION_FORMATS = ("mcq", "fill_blank", "short_answer")

_QUESTION_FORMAT_SYNONYMS = {
    "multiple_choice": "mcq",
    "multiple choice": "mcq",
    "mcqs": "mcq",
    "fill in the blank": "fill_blank",
    "fill_in_the_blank": "fill_blank",
    "fill-in-the-blank": "fill_blank",
    "blank": "fill_blank",
    "short answer": "short_answer",
    "shortanswer": "short_answer",
}


def parse_question_formats(raw: str | None) -> list[str] | None:
    """Parse the comma-separated ``question_formats`` upload parameter.

    Returns None for "not specified", which retrieval reads as "this file can
    serve any format" — the behaviour every file uploaded before this existed
    already has.
    """
    if not raw or not raw.strip():
        return None

    formats = []
    for part in raw.split(","):
        key = part.strip().lower().replace("-", "_")
        key = _QUESTION_FORMAT_SYNONYMS.get(key, _QUESTION_FORMAT_SYNONYMS.get(part.strip().lower(), key))
        if key:
            formats.append(key)

    if not formats:
        raise ValueError("question_formats cannot be empty")

    unknown = [f for f in formats if f not in _VALID_QUESTION_FORMATS]
    if unknown:
        raise ValueError(
            f"Unsupported question format(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(_VALID_QUESTION_FORMATS)}"
        )

    return list(dict.fromkeys(formats))


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
    # 'basic', not 'beginner': documents_difficulty_check allows exactly
    # basic|intermediate|advanced. This function used to emit 'beginner', which
    # is not in that set, so every upload that did not name a difficulty was
    # rejected by Postgres at insert time — the whole lecturer upload path.
    allowed = {"basic", "intermediate", "advanced"}
    synonyms = {
        "beginner": "basic",
        "easy": "basic",
        "medium": "intermediate",
        "normal": "intermediate",
        "hard": "advanced",
    }

    if not difficulty:
        return "basic"

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


def parse_target_modes(raw: str | None, default_mode: str) -> list[str] | None:
    """Parse the comma-separated ``target_modes`` parameter.

    Returns None when nothing is supplied, which ingest_document reads as
    "index for default_mode alone" — the pre-existing behaviour, so a caller
    that predates this parameter is unaffected.
    """
    if not raw or not raw.strip():
        return None

    modes = [m.strip().lower() for m in raw.split(",") if m.strip()]
    if not modes:
        raise ValueError("target_modes cannot be empty")

    unknown = [m for m in modes if m not in _VALID_MODES]
    if unknown:
        raise ValueError(
            f"Unsupported target mode(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(_VALID_MODES)}"
        )

    # Deduplicate while preserving the caller's ordering.
    return list(dict.fromkeys(modes))


async def ingest_document(
    file, course_id, doc_type, default_mode, difficulty, version, user_id,
    target_modes: list[str] | None = None,
    is_published: bool = True,
    topic: str | None = None,
    question_formats: list[str] | None = None,
):
    doc_type = _resolve_doc_type(doc_type)
    default_mode = _resolve_default_mode(default_mode, doc_type)
    difficulty = _resolve_difficulty(difficulty)

    supabase = get_supabase()

    row = {
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

    # Written separately so a database that predates
    # 006_add_mode_aware_publishable_documents.sql still accepts the insert — the
    # retry below drops them rather than failing the upload.
    optional = {
        "target_modes": target_modes or [default_mode],
        "is_published": is_published,
        "topic": topic,
        # Only meaningful for Review material, and only stored when the
        # lecturer actually chose formats. A NULL means "any format", which is
        # what every file uploaded before this feature existed should keep.
        "question_formats": question_formats or None,
    }

    # 1. Register document (FAST)
    try:
        doc_response = supabase.table("documents").insert({**row, **optional}).execute()
    except Exception as exc:  # noqa: BLE001 — columns may not exist yet
        logger.warning(
            "Document insert rejected the mode/publish columns; falling back. "
            "Apply 006_add_mode_aware_publishable_documents.sql: %s",
            exc,
        )
        doc_response = supabase.table("documents").insert(row).execute()

    if not doc_response.data:
        raise RuntimeError("Failed to register document in database.")

    doc = doc_response.data[0]
    document_id = doc["id"]

    # Read file content now — the upload stream closes after the response
    file_content = await file.read()
    filename = file.filename

    # Dispatch to the dedicated ingestion_tasks queue so spaCy / unstructured
    # run in an isolated Celery worker, never inside the API process.
    # File bytes are base64-encoded because Celery's JSON serialiser
    # cannot handle raw bytes.
    from app.ai.tasks import process_ingestion_task
    process_ingestion_task.apply_async(
        args=[
            document_id,
            base64.b64encode(file_content).decode(),
            filename,
            course_id,
            doc_type,
            default_mode,
            difficulty,
        ],
        queue="ingestion_tasks",
    )

    return {
        "document_id": document_id,
        "status": "processing",
        "message": "Document ingestion started.",
    }


# `document_chunks.mode` carries a check constraint from an older vocabulary
# that named the mode after the document type. It permits 'practice' but not
# 'application', which every other table and the whole application use. Until
# sql/001_allow_application_mode_in_document_chunks.sql is applied, writing the
# correct value fails; this maps to the value the constraint still accepts.
# Once the migration lands the first insert succeeds and this is never reached,
# so the shim retires itself.
_LEGACY_CHUNK_MODE = {"application": "practice"}

# PostgreSQL check-constraint violation.
_CHECK_VIOLATION = "23514"


def _store_chunk_rows(supabase, rows: list, mode: str, filename: str) -> None:
    """Persist chunk bookkeeping, tolerating the legacy mode constraint.

    These rows are bookkeeping only — retrieval reads Pinecone, and admin
    deletion uses them to find vectors to remove. They are written *after* the
    vectors are already live, so a failure here must never fail an otherwise
    complete ingestion: doing so marks a document `failed` while its vectors
    serve queries, which is the worst of both states.
    """
    try:
        supabase.table("document_chunks").insert(rows).execute()
        return
    except Exception as first_error:
        legacy = _LEGACY_CHUNK_MODE.get(mode)
        is_constraint = _CHECK_VIOLATION in str(first_error)
        if not legacy or not is_constraint:
            logger.warning(
                "Chunk bookkeeping failed for %s; vectors are live and the "
                "document is usable, but admin deletion cannot find them: %s",
                filename, first_error,
            )
            return

    try:
        supabase.table("document_chunks").insert(
            [{**row, "mode": legacy} for row in rows]
        ).execute()
        logger.warning(
            "Stored chunk rows for %s with mode=%r instead of %r — the "
            "document_chunks check constraint predates the 'application' mode. "
            "Apply sql/001_allow_application_mode_in_document_chunks.sql to fix.",
            filename, legacy, mode,
        )
    except Exception as retry_error:
        logger.warning(
            "Chunk bookkeeping failed for %s even with the legacy mode; "
            "vectors are live and the document is usable: %s",
            filename, retry_error,
        )


_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _content_type_for(filename: str) -> str:
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def _resolve_target_modes(supabase, document_id: str, default_mode: str) -> list[str]:
    """Modes this document should be indexed for.

    Falls back to ``[default_mode]`` whenever target_modes is absent, empty or
    unreadable — including before 006_add_mode_aware_publishable_documents.sql is
    applied. Indexing into no namespace at all would ingest a document that can
    never be retrieved, which is worse than indexing into one.
    """
    try:
        rows = (
            supabase.table("documents")
            .select("target_modes")
            .eq("id", document_id)
            .execute()
            .data
        )
    except Exception as exc:  # noqa: BLE001 — column may not exist yet
        logger.warning(
            "target_modes unavailable for %s; indexing %r only: %s",
            document_id, default_mode, exc,
        )
        return [default_mode]

    modes = (rows[0].get("target_modes") if rows else None) or []
    valid = [m for m in modes if m in _VALID_MODES]

    if not valid:
        return [default_mode]

    # Keep the default first so it is indexed even if a later namespace fails.
    return sorted(set(valid), key=lambda m: (m != default_mode, m))


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

    Parsing preserves equations and document structure (see
    :mod:`app.ai.ingestion.math_parser`) and chunking is parent-child, so each
    vector carries the metadata a citation needs. Vectors land in a versioned
    namespace; the previous generation is left untouched and stays queryable
    until every document has been re-ingested.
    """
    supabase = get_supabase()

    try:
        # 1. Archive the original upload before touching it. Ingestion used to
        # parse the bytes and drop them, which made re-ingestion impossible
        # without the lecturer re-uploading the file. Best-effort: a storage
        # failure must not cost an otherwise good ingestion.
        storage_path = await asyncio.to_thread(
            upload_course_document,
            course_id, document_id, filename, file_content, _content_type_for(filename),
        )
        if storage_path:
            try:
                supabase.table("documents").update({"storage_path": storage_path}).eq(
                    "id", document_id
                ).execute()
            except Exception as exc:  # noqa: BLE001 — column may not exist yet
                logger.warning("Could not record storage_path for %s: %s", filename, exc)

        # 2. Parse into structured blocks, equations intact (CPU bound)
        blocks = await asyncio.to_thread(parse_blocks, file_content, filename)

        # 3. A PDF text layer cannot represent typeset maths — recover flagged
        # pages before chunking, so the recovered formulas are chunked and
        # embedded like any other content (network I/O; no-op without a provider).
        blocks = await apply_math_ocr(blocks, file_content, filename)

        # 4. Chunk parent-child, nothing dropped for being short (CPU bound)
        chunks = await asyncio.to_thread(
            build_chunks,
            blocks,
            document_id=document_id,
            source_filename=filename,
            mode=default_mode,
            difficulty=difficulty,
            course_id=course_id,
        )

        if not chunks:
            supabase.table("documents").update({"status": "no_content"}).eq(
                "id", document_id
            ).execute()
            return

        # 5. Embed the breadcrumbed text so each vector knows its topic.
        # Done once regardless of how many modes the document serves — the
        # vectors are identical, only the namespace and the stamped mode differ.
        embeddings = await embed_chunks([c["embed_text"] for c in chunks])

        # 6. Upsert into the current-generation namespace of every target mode
        modes = _resolve_target_modes(supabase, document_id, default_mode)

        for mode in modes:
            namespace = namespace_for(course_id, mode)
            pinecone_ids = await asyncio.to_thread(
                upsert_chunks, embeddings, chunks, namespace=namespace, mode=mode
            )

            # 7. Batch store chunk metadata, per namespace
            batch_metadata = [
                {
                    "document_id": document_id,
                    "pinecone_id": pid,
                    "pinecone_namespace": namespace,
                    "mode": mode,
                    "topic": chunk.get("topic"),
                    "difficulty": difficulty,
                    "content_hash": hashlib.sha256(chunk["text"].encode()).hexdigest(),
                }
                for chunk, pid in zip(chunks, pinecone_ids)
            ]

            if batch_metadata:
                _store_chunk_rows(supabase, batch_metadata, mode, filename)

        # 8. Mark Ready
        supabase.table("documents").update(
            {"status": "ready", "total_chunks": len(chunks)}
        ).eq("id", document_id).execute()

        logger.info(
            "Ingested %s: %d chunks (%d with maths) -> %s",
            filename,
            len(chunks),
            sum(1 for c in chunks if c.get("has_math")),
            namespace,
        )

    except Exception as e:
        logger.error(f"Background ingestion failed for {document_id}: {str(e)}", exc_info=True)
        supabase.table("documents").update({"status": "failed"}).eq(
            "id", document_id
        ).execute()
