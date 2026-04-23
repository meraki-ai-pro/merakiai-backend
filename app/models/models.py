from pydantic import BaseModel, EmailStr


class SessionCreateRequest(BaseModel):
    course_id: str
    mode: str = "learn"
    prefers_video: bool = False


class RagTurnRequest(BaseModel):
    session_id: str
    mode: str = "learn"  # learn only here
    message: str


class ModeSessionStartRequest(BaseModel):
    session_id: str
    mode: str
    session_type: str
    difficulty: str = "Basic"
    total_items: int | None = None


class ModeSessionTurnRequest(BaseModel):
    message: str


class VideoToggle(BaseModel):
    prefers_video: bool
    


class SignUpPayload(BaseModel):
    email: EmailStr
    password: str
    first_name: str
    last_name: str
    university_name: str
    region: str
    country: str


class LoginPayload(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordPayload(BaseModel):
    email: EmailStr
    redirect_to: str | None = None

class AvatarSelectRequest(BaseModel):
    avatar_id: str  # "amy" | "josh"
    
class TextTurnRequest(BaseModel):
    session_id: str
    message: str
    response_format: str | None = None

class SessionModeUpdate(BaseModel):
    current_mode: str  # learn|application|review

class UpdatePasswordPayload(BaseModel):
    new_password: str

class SessionSurveyPayload(BaseModel):
    session_id: str
    clarity_rating: int
    helpfulness_rating: int
    confidence_rating: int
    overall_rating: int

class UserFeedbackPayload(BaseModel):
    session_id: str | None = None
    feedback_type: str  # bug|suggestion|content|ux|other
    message: str
