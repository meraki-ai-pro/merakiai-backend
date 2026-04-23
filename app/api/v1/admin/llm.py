from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.auth import admin_guard
from app.core.llm_config import (
    DEFAULTS,
    VALID_MODES,
    get_all_configs,
    reset_all_configs,
    update_mode_config,
)
from app.db.supabase import get_supabase

router = APIRouter(prefix="/llm", tags=["Admin – LLM"])

# Supported Claude models (kept in sync with Anthropic's current lineup)
AVAILABLE_MODELS = [
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]


class ModeConfigUpdate(BaseModel):
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@router.get("/config")
def get_llm_config(_user=Depends(admin_guard)):
    """Return the active per-mode LLM configuration (defaults + any admin overrides)."""
    return {
        "config": get_all_configs(),
        "defaults": DEFAULTS,
        "available_models": AVAILABLE_MODELS,
    }


@router.patch("/config/{mode}")
def update_llm_mode_config(mode: str, payload: ModeConfigUpdate, _user=Depends(admin_guard)):
    """Override LLM settings for a specific mode. Changes persist in llm_config.json."""
    if mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"Mode must be one of {sorted(VALID_MODES)}")

    updates: Dict[str, Any] = {}
    if payload.model is not None:
        if payload.model not in AVAILABLE_MODELS:
            raise HTTPException(status_code=400, detail=f"Model must be one of {AVAILABLE_MODELS}")
        updates["model"] = payload.model
    if payload.temperature is not None:
        if not (0.0 <= payload.temperature <= 1.0):
            raise HTTPException(status_code=400, detail="temperature must be between 0 and 1")
        updates["temperature"] = payload.temperature
    if payload.max_tokens is not None:
        if not (1 <= payload.max_tokens <= 8192):
            raise HTTPException(status_code=400, detail="max_tokens must be between 1 and 8192")
        updates["max_tokens"] = payload.max_tokens

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    new_config = update_mode_config(mode, updates)
    return {"status": "ok", "mode": mode, "config": new_config}


@router.post("/config/reset")
def reset_llm_config(_user=Depends(admin_guard)):
    """Restore all mode configs to hardcoded defaults."""
    if _user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Only super admins can reset LLM config")
    defaults = reset_all_configs()
    return {"status": "ok", "config": defaults}


@router.get("/usage")
def get_llm_usage(days: int = Query(30, ge=1, le=365), _user=Depends(admin_guard)):
    """Usage and cost-proxy stats from request_metrics, grouped by mode and format."""
    from datetime import datetime, timedelta, timezone
    from collections import defaultdict

    sb = get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    rows = sb.table("request_metrics").select(
        "mode, response_format, ai_processing_ms, video_generation_ms, processing_time_sec, created_at"
    ).gte("created_at", cutoff).execute().data or []

    by_mode: dict = defaultdict(lambda: {
        "requests": 0, "ai_ms": [], "video_ms": [], "text_requests": 0, "video_requests": 0
    })

    for r in rows:
        m = r.get("mode") or "unknown"
        fmt = r.get("response_format") or "text"
        by_mode[m]["requests"] += 1
        if fmt == "video":
            by_mode[m]["video_requests"] += 1
        else:
            by_mode[m]["text_requests"] += 1
        if r.get("ai_processing_ms"):
            by_mode[m]["ai_ms"].append(r["ai_processing_ms"])
        if r.get("video_generation_ms"):
            by_mode[m]["video_ms"].append(r["video_generation_ms"])

    def _avg(vals):
        return round(sum(vals) / len(vals), 1) if vals else None

    summary = {}
    for m, v in by_mode.items():
        summary[m] = {
            "total_requests": v["requests"],
            "text_requests": v["text_requests"],
            "video_requests": v["video_requests"],
            "avg_ai_ms": _avg(v["ai_ms"]),
            "avg_video_ms": _avg(v["video_ms"]),
            "model_in_use": get_all_configs().get(m, {}).get("model", "unknown"),
        }

    return {
        "period_days": days,
        "total_requests": len(rows),
        "by_mode": summary,
    }
