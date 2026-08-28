import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import auth_guard
from app.db.supabase import get_user_client
from app.models.models import SessionSurveyPayload, UserFeedbackPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["Feedback"])


@router.post("/session-survey")
def submit_session_survey(payload: SessionSurveyPayload, user=Depends(auth_guard)):
    supabase = get_user_client(user["token"])

    # RLS on sessions filters to owned rows, so empty data means not found or not owned.
    if not supabase.table("sessions").select("id").eq("id", payload.session_id).execute().data:
        raise HTTPException(status_code=404, detail="Session not found")

    ins = supabase.table("session_surveys").insert({
        "session_id": payload.session_id,
        "user_id": user["id"],
        "clarity_rating": payload.clarity_rating,
        "helpfulness_rating": payload.helpfulness_rating,
        "confidence_rating": payload.confidence_rating,
        "overall_rating": payload.overall_rating,
    }).execute()
    return {"status": "ok", "id": ins.data[0]["id"] if ins.data else None}


@router.post("/user-feedback")
def submit_user_feedback(payload: UserFeedbackPayload, user=Depends(auth_guard)):
    supabase = get_user_client(user["token"])

    session_course_id = None
    if payload.session_id:
        session = (
            supabase.table("sessions").select("id, course_id")
            .eq("id", payload.session_id).execute().data
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        session_course_id = session[0].get("course_id")

    # The session's own course beats the client's, when there is a session. A
    # student who switched courses in another tab would otherwise file a
    # complaint about the lesson they are reading against a course they are
    # not on.
    course_id = session_course_id or payload.course_id

    row = {
        "user_id": user["id"],
        "session_id": payload.session_id,
        "feedback_type": payload.feedback_type,
        "message": payload.message,
    }

    try:
        ins = supabase.table("user_feedback").insert({**row, "course_id": course_id}).execute()
    except Exception as exc:  # noqa: BLE001 — sql/013 may not be applied yet
        logger.warning("user_feedback rejected course_id; storing without it: %s", exc)
        ins = supabase.table("user_feedback").insert(row).execute()

    return {"status": "ok", "id": ins.data[0]["id"] if ins.data else None}
