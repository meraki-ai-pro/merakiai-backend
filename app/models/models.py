from pydantic import AnyHttpUrl, BaseModel, EmailStr, Field


class SessionCreateRequest(BaseModel):
    course_id: str = Field(..., max_length=100)
    mode: str = Field("learn", max_length=50)
    prefers_video: bool = False


class RagTurnRequest(BaseModel):
    session_id: str = Field(..., max_length=100)
    mode: str = Field("learn", max_length=50)  # learn only here
    # L-4: cap message size — prevents multi-MB strings being forwarded to AI APIs
    message: str = Field(..., min_length=1, max_length=10_000)


class ModeSessionStartRequest(BaseModel):
    session_id: str = Field(..., max_length=100)
    mode: str = Field(..., max_length=50)
    # Optional since the scenario-topic picker was removed from Assessment
    # mode: an assessment session has no topic to choose, so the client sends
    # nothing and the server fills in the neutral placeholder. Review sessions
    # still send the question format here and it is still required for them —
    # enforced in the route, where the mode is known.
    session_type: str | None = Field(None, max_length=50)
    difficulty: str = Field("Basic", max_length=50)
    total_items: int | None = Field(None, ge=1, le=50)


class ModeSessionTurnRequest(BaseModel):
    # L-4: cap message size — prevents multi-MB student answers
    message: str = Field(..., min_length=1, max_length=10_000)


class VideoToggle(BaseModel):
    prefers_video: bool


class SignUpPayload(BaseModel):
    email: EmailStr
    # L-4: realistic length caps on all name/location fields
    password: str = Field(..., min_length=8, max_length=128)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    university_name: str = Field(..., max_length=200)
    region: str = Field(..., max_length=100)
    country: str = Field(..., max_length=100)


class LoginPayload(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class ForgotPasswordPayload(BaseModel):
    email: EmailStr
    redirect_to: AnyHttpUrl | None = None  # must be a valid URL; origin checked against ALLOWED_ORIGINS in route


class ResetPasswordPayload(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128)


class AvatarSelectRequest(BaseModel):
    avatar_id: str = Field(..., max_length=100)


class TextTurnRequest(BaseModel):
    session_id: str = Field(..., max_length=100)
    message: str = Field(..., min_length=1, max_length=10_000)
    response_format: str | None = Field(None, max_length=50)


class SessionModeUpdate(BaseModel):
    current_mode: str = Field(..., max_length=50)  # learn|application|review


class SessionTitleUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)


class UpdatePasswordPayload(BaseModel):
    # Required, not optional. Without it a stolen access token is enough to
    # take an account over permanently — the thief sets a new password and the
    # owner is locked out of their own tenancy. Proving the current password
    # is what makes the token alone insufficient.
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class UpdateProfilePayload(BaseModel):
    """Fields a user may change about themselves.

    Deliberately excludes ``email`` and ``role``. Email is an auth identity and
    changing it needs a verification round trip Supabase owns; role is granted
    by an admin (see /admin/users/{id}/role) and a self-service field here
    would be a straight privilege escalation.
    """

    first_name: str | None = Field(None, min_length=1, max_length=100)
    last_name: str | None = Field(None, min_length=1, max_length=100)
    university_name: str | None = Field(None, max_length=200)
    region: str | None = Field(None, max_length=100)
    country: str | None = Field(None, max_length=100)


class SessionSurveyPayload(BaseModel):
    session_id: str = Field(..., max_length=100)
    clarity_rating: int = Field(..., ge=1, le=5)
    helpfulness_rating: int = Field(..., ge=1, le=5)
    confidence_rating: int = Field(..., ge=1, le=5)
    overall_rating: int = Field(..., ge=1, le=5)


class UserFeedbackPayload(BaseModel):
    session_id: str | None = Field(None, max_length=100)
    # What the student was studying. "The derivative in step 3 is wrong" is not
    # actionable without it, and most feedback is sent outside a session so
    # session_id cannot stand in.
    course_id: str | None = Field(None, max_length=100)
    feedback_type: str = Field(..., max_length=50)  # bug|suggestion|content|ux|other
    # L-4: cap feedback message to prevent abuse / huge DB writes
    message: str = Field(..., min_length=1, max_length=5_000)
