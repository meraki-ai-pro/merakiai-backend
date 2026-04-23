from fastapi import APIRouter, Depends, HTTPException, UploadFile, BackgroundTasks
from .service import ingest_document
from app.core.auth import admin_guard
from app.db.supabase import get_user_client

router = APIRouter(prefix="/ingestion", tags=["Ingestion"])


@router.post("/documents")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    course_id: str,
    doc_type: str,
    default_mode: str,
    difficulty: str,
    version: str,
    user=Depends(admin_guard),
):
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
        background_tasks=background_tasks,
    )
