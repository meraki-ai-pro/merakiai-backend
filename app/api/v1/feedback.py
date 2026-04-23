from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import auth_guard
from app.db.supabase import get_user_client
from app.models.models import SessionSurveyPayload, UserFeedbackPayload

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

    if payload.session_id:
        if not supabase.table("sessions").select("id").eq("id", payload.session_id).execute().data:
            raise HTTPException(status_code=404, detail="Session not found")

    ins = supabase.table("user_feedback").insert({
        "user_id": user["id"],
        "session_id": payload.session_id,
        "feedback_type": payload.feedback_type,
        "message": payload.message,
    }).execute()
    return {"status": "ok", "id": ins.data[0]["id"] if ins.data else None}
