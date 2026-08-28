from fastapi import APIRouter, Depends, HTTPException, UploadFile
from .service import ingest_document, parse_target_modes
from app.core.auth import admin_guard
from app.db.supabase import get_user_client

router = APIRouter(prefix="/ingestion", tags=["Ingestion"])

# M-4: 50 MB hard cap on document uploads
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_ALLOWED_EXTENSIONS = {"pdf", "docx", "doc", "pptx"}


@router.post("/documents")
async def upload_document(
    file: UploadFile,
    course_id: str,
    doc_type: str,
    default_mode: str,
    difficulty: str,
    version: str,
    target_modes: str | None = None,
    is_published: bool = True,
    topic: str | None = None,
    user=Depends(admin_guard),
):
    """Ingest a knowledge file.

    ``target_modes`` is a comma-separated list (e.g. "learn,review") — one file
    of worked examples is legitimately both Learn and Review material, and it is
    embedded once then upserted into each mode's namespace. Omit it to index for
    ``default_mode`` alone, which is the pre-existing behaviour.

    ``is_published`` defaults to true so the current admin flow is unchanged;
    the lecturer UI will pass false to stage a file and test-query it first.
    """
    # M-4: validate file extension before reading any bytes
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '.{ext}'. Allowed: {', '.join(_ALLOWED_EXTENSIONS)}",
        )

    # M-4: read the full file once, enforce size limit, then validate magic bytes
    contents = await file.read()
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the 50 MB upload limit ({len(contents) // (1024*1024)} MB received).",
        )

    # Magic-byte validation — extension alone is not trustworthy
    if ext == "pdf" and not contents[:4] == b"%PDF":
        raise HTTPException(status_code=415, detail="File does not appear to be a valid PDF.")
    if ext in ("docx", "doc") and not contents[:4] == b"PK\x03\x04":
        raise HTTPException(status_code=415, detail="File does not appear to be a valid DOCX.")

    # Reset so downstream code (parser) can read from the beginning
    await file.seek(0)

    # Validate course exists before doing any heavy work
    supabase = get_user_client(user["token"])
    course = supabase.table("courses").select("id").eq("id", course_id).execute()
    if not course.data:
        raise HTTPException(status_code=404, detail=f"Course '{course_id}' not found")

    try:
        modes = parse_target_modes(target_modes, default_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await ingest_document(
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
    )
