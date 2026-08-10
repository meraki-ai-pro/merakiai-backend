"""
Runtime-switchable API keys for the external media services.

Mirrors the `llm_config.py` pattern: admin overrides are persisted to a JSON
file next to the app root and merged over the environment. Because the API
process and the Celery media workers share the same filesystem, an override
written by the API is picked up by the workers on their next media call — the
cache is invalidated by file mtime, so no restart is required.

Every media service reads its key through `get_key()` at call time instead of
capturing `os.getenv()` at import, so a rotated key takes effect immediately.

Secrets are NEVER returned in full by the status API — only a masked preview.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "media_config.json"
_lock = threading.Lock()

# (mtime, parsed-overrides) — reloaded whenever the file changes on disk so a
# write from another process (the API) is seen by the workers, and vice-versa.
_cache_mtime: Optional[float] = None
_cache_data: Dict[str, str] = {}

# The keys an admin may rotate from the dashboard. `env` is the os.environ name
# the rest of the app already reads.
MANAGED_KEYS: List[Dict[str, str]] = [
    {"name": "DID_API_KEY",        "label": "D-ID API Key",       "service": "D-ID — real-time avatar video"},
    {"name": "TAVUS_API_KEY",      "label": "Tavus API Key",      "service": "Tavus — avatar video fallback"},
    {"name": "ELEVENLABS_API_KEY", "label": "ElevenLabs API Key", "service": "ElevenLabs — text-to-speech"},
    {"name": "OPENAI_API_KEY",     "label": "OpenAI API Key",     "service": "OpenAI — speech-to-text & embeddings"},
]

_MANAGED_NAMES = {k["name"] for k in MANAGED_KEYS}


def _load_overrides() -> Dict[str, str]:
    """Return on-disk overrides, reloading only when the file's mtime changes."""
    global _cache_mtime, _cache_data
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
    except FileNotFoundError:
        _cache_mtime, _cache_data = None, {}
        return _cache_data

    if mtime == _cache_mtime:
        return _cache_data

    with _lock:
        # Re-check inside the lock in case another thread just reloaded.
        try:
            mtime = _CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            _cache_mtime, _cache_data = None, {}
            return _cache_data
        if mtime == _cache_mtime:
            return _cache_data
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            _cache_data = {str(k): str(v) for k, v in data.items() if v}
        except Exception:
            _cache_data = {}
        _cache_mtime = mtime
        return _cache_data


def get_key(name: str) -> Optional[str]:
    """Return the effective value for `name`: admin override wins, else env.

    An empty string is treated as unset so a blank override never shadows a
    real environment value by accident.
    """
    override = _load_overrides().get(name)
    if override:
        return override
    val = os.getenv(name)
    return val or None


def _mask(value: str) -> str:
    """Show only enough to confirm which key is loaded — never the secret."""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}…{value[-4:]}"


def get_status() -> List[Dict[str, Any]]:
    """Per-managed-key status for the admin UI. Never leaks the full secret."""
    overrides = _load_overrides()
    out: List[Dict[str, Any]] = []
    for meta in MANAGED_KEYS:
        name = meta["name"]
        if overrides.get(name):
            source, value = "override", overrides[name]
        elif os.getenv(name):
            source, value = "env", os.environ[name]
        else:
            source, value = "unset", ""
        out.append({
            **meta,
            "is_set": bool(value),
            "source": source,
            "masked": _mask(value) if value else None,
        })
    return out


def set_keys(updates: Dict[str, str]) -> List[Dict[str, Any]]:
    """Persist one or more key overrides atomically. Unknown names are rejected.

    A value of "" (empty) clears the override, reverting to the env value.
    """
    unknown = [k for k in updates if k not in _MANAGED_NAMES]
    if unknown:
        raise ValueError(f"Unknown media keys: {unknown}")

    global _cache_mtime, _cache_data
    with _lock:
        current = dict(_load_overrides())
        for name, value in updates.items():
            value = (value or "").strip()
            if value:
                current[name] = value
            else:
                current.pop(name, None)  # clear override → fall back to env
        # NB: we deliberately do NOT touch os.environ — get_key() reads the
        # override from this store first, so mutating the process env would only
        # risk clobbering the original .env value that a cleared override falls
        # back to.

        tmp_path = _CONFIG_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
        tmp_path.replace(_CONFIG_PATH)

        _cache_data = current
        try:
            _cache_mtime = _CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            _cache_mtime = None

    return get_status()
