from contextlib import asynccontextmanager

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from app.config import load_env

load_env()


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
