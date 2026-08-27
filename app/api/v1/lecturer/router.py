"""Lecturer surface.

Every route here goes through lecturer_guard, and every course-scoped route
additionally calls assert_course_owner(). The guard proves the caller is a
lecturer; only the ownership check decides which course they may touch.
"""

from fastapi import APIRouter

from app.api.v1.lecturer.analytics import router as analytics_router
from app.api.v1.lecturer.courses import router as courses_router
from app.api.v1.lecturer.knowledge import router as knowledge_router
from app.api.v1.lecturer.students import router as students_router
from app.api.v1.lecturer.voices import course_voice_router
from app.api.v1.lecturer.voices import router as voices_router

router = APIRouter(prefix="/lecturer")

router.include_router(courses_router)
router.include_router(knowledge_router)
router.include_router(students_router)
router.include_router(analytics_router)
router.include_router(voices_router)
router.include_router(course_voice_router)
