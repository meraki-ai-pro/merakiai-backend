from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import admin_guard
from app.core.media_config import get_status, set_keys

router = APIRouter(prefix="/media", tags=["Admin – Media"])


class MediaKeysUpdate(BaseModel):
    # Any subset may be provided. An empty string clears that override and
    # reverts the service to its environment / .env value.
    DID_API_KEY: str | None = Field(default=None)
    TAVUS_API_KEY: str | None = Field(default=None)
    ELEVENLABS_API_KEY: str | None = Field(default=None)
    OPENAI_API_KEY: str | None = Field(default=None)


@router.get("/keys")
def get_media_keys(_user=Depends(admin_guard)):
    """Masked status for every managed media-service key (never the full secret)."""
    return {"keys": get_status()}


@router.patch("/keys")
def update_media_keys(payload: MediaKeysUpdate, _user=Depends(admin_guard)):
    """Rotate one or more media-service API keys (admin only).

    Consistent with the other admin_guard mutations (LLM config, user role/delete).
    Overrides persist in media_config.json and are picked up by the media
    workers on their next call — no restart required.
    """
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No keys to update")

    try:
        keys = set_keys(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"status": "ok", "keys": keys}
