from fastapi import APIRouter, Depends, HTTPException, UploadFile
from .service import ingest_document
from app.core.auth import admin_guard
from app.db.supabase import get_user_client

router = APIRouter(prefix="/ingestion", tags=["Ingestion"])

# M-4: 50 MB hard cap on document uploads
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_ALLOWED_EXTENSIONS = {"pdf", "docx", "doc"}


@router.post("/documents")
async def upload_document(
    file: UploadFile,
    course_id: str,
    doc_type: str,
    default_mode: str,
    difficulty: str,
    version: str,
    user=Depends(admin_guard),
):
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

    return await ingest_document(
        file=file,
        course_id=course_id,
        doc_type=doc_type,
        default_mode=default_mode,
        difficulty=difficulty,
        version=version,
        user_id=user["id"],
    )
