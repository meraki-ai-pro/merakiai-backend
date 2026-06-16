# ── Secrets MUST be loaded before any app module is imported ─────────────────
# Several modules read os.getenv() at module level (e.g. webhook secrets,
# Supabase keys). If load_env() ran after those imports the values would be
# empty strings even with a valid .env / AWS secret in place.
import os  # noqa: E402 (intentional early import)
from app.config import load_env
load_env()
# ─────────────────────────────────────────────────────────────────────────────

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

from app.ai.ingestion.router import router as ingestion_router
from app.ai.rag.router import router as rag_router
from app.api.v1.auth import router as auth_router
from app.api.v1.sessions.router import router as sessions_router
from app.api.v1.users.router import router as users_router
from app.api.v1.feedback import router as feedback_router
from app.ai.rag.modes_sessions.router import router as mode_sessions_router
from app.api.v1.ws.router import router as ws_router
from app.api.v1.webhooks.did import router as did_webhook_router
from app.api.v1.webhooks.tavus import router as tavus_webhook_router
from app.api.v1.admin.router import router as admin_router
from app.api.v1.video.router import router as video_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — nothing to initialise; WebSocketManager is a lazy singleton
    yield
    # Shutdown — disconnect all active WebSocket sessions cleanly
    from app.core.websocket_manager import manager
    for session_id in list(manager._connections.keys()):
        await manager.disconnect(session_id)


app = FastAPI(
    title="Meraki AI - AI Assistant Lecturer",
    version="1.0.0",
    lifespan=lifespan,
)

_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

# Vercel preview deployments get a unique subdomain per branch/PR
# (e.g. yourapp-git-feature-abc-username.vercel.app).
# VERCEL_APP_NAME should match the Vercel project slug so only your
# project's previews are allowed, not all of *.vercel.app.
# Set to empty string to disable preview-deployment CORS entirely.
_vercel_app = os.getenv("VERCEL_APP_NAME", "")  # e.g. "meraki-ai"
_vercel_origin_regex = (
    rf"https://{_vercel_app}-.*\.vercel\.app" if _vercel_app else None
)

# L-2: Reject requests with unrecognised Host headers (Host-injection hardening).
# Set ALLOWED_HOSTS in .env / AWS Secrets to a comma-separated list of your
# real domain names (e.g. "api.yourdomain.com,localhost,127.0.0.1").
# Default to "*" so LAN access via a machine IP works without needing
# per-machine host allowlists.
_raw_hosts = os.getenv("ALLOWED_HOSTS", "*")
_allowed_hosts = [h.strip() for h in _raw_hosts.split(",") if h.strip()] or ["*"]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=_vercel_origin_regex,
    allow_credentials=True,
    # M-8: explicit allowlists — never expose all methods/headers to all origins
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for any unhandled exception.

    Logs the full traceback internally and returns a generic 500 to the client
    so internal details (stack traces, DB errors, file paths) are never leaked.
    """
    logger.exception(
        "Unhandled exception on %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected error occurred. Please try again later."},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


app.include_router(ingestion_router)
app.include_router(rag_router)
app.include_router(auth_router)
app.include_router(sessions_router)
app.include_router(users_router)
app.include_router(feedback_router)
app.include_router(mode_sessions_router)
app.include_router(ws_router)
app.include_router(did_webhook_router)
app.include_router(tavus_webhook_router)
app.include_router(admin_router)
app.include_router(video_router)
