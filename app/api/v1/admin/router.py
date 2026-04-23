from fastapi import APIRouter

from app.api.v1.admin.analytics import router as analytics_router
from app.api.v1.admin.courses import router as courses_router
from app.api.v1.admin.documents import router as documents_router
from app.api.v1.admin.llm import router as llm_router
from app.api.v1.admin.users import router as users_router

router = APIRouter(prefix="/admin")

router.include_router(analytics_router)
router.include_router(users_router)
router.include_router(documents_router)
router.include_router(courses_router)
router.include_router(llm_router)
