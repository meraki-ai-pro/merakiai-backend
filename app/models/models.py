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
    session_type: str = Field(..., max_length=50)
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
    new_password: str = Field(..., min_length=8, max_length=128)


class SessionSurveyPayload(BaseModel):
    session_id: str = Field(..., max_length=100)
    clarity_rating: int = Field(..., ge=1, le=5)
    helpfulness_rating: int = Field(..., ge=1, le=5)
    confidence_rating: int = Field(..., ge=1, le=5)
    overall_rating: int = Field(..., ge=1, le=5)


class UserFeedbackPayload(BaseModel):
    session_id: str | None = Field(None, max_length=100)
    feedback_type: str = Field(..., max_length=50)  # bug|suggestion|content|ux|other
    # L-4: cap feedback message to prevent abuse / huge DB writes
    message: str = Field(..., min_length=1, max_length=5_000)
