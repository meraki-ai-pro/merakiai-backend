from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "llm_config.json"
_lock = threading.Lock()

# M-18: in-memory cache so every LLM call doesn't hit the disk.
# Invalidated on every write; safe because all writes go through _lock.
_overrides_cache: Optional[Dict[str, Any]] = None

VALID_MODES = {"learn", "application", "review", "review_generation"}

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "learn": {
        "model": "claude-opus-4-6",
        "temperature": 0.4,
        "max_tokens": 1000,
    },
    "application": {
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "max_tokens": 1500,
        "output_config": {"effort": "low"},
    },
    "review": {
        "model": "claude-haiku-4-5-20251001",
        "temperature": 0.1,
        "max_tokens": 600,
    },
    "review_generation": {
        "model": "claude-haiku-4-5-20251001",
        "temperature": 0.2,
        "max_tokens": 700,
    },
    # Manim scene generation. Deliberately NOT reusing "learn": a chat answer
    # fits in 1000 tokens, a Manim scene does not, and a scene truncated
    # mid-statement fails to parse rather than degrading gracefully.
    #
    # Low temperature because this is code — mathematical accuracy matters far
    # more than variety, and Proposal §10 names notation errors as the risk
    # that would actually damage the pilot.
    "scene_generation": {
        "model": "claude-opus-4-6",
        "temperature": 0.2,
        "max_tokens": 8000,
    },
}


def _load_overrides() -> Dict[str, Any]:
    """Return the on-disk overrides, using the in-memory cache when available.

    M-18: reads from cache on the hot path; only hits disk on first load or
    after a write (cache is cleared inside _lock in update/reset).
    """
    global _overrides_cache
    # Fast path: cache is warm (no lock needed for reads after first load)
    if _overrides_cache is not None:
        return _overrides_cache
    # Slow path: first read or post-invalidation
    with _lock:
        # Double-check inside the lock — another thread may have populated it
        if _overrides_cache is not None:
            return _overrides_cache
        if not _CONFIG_PATH.exists():
            _overrides_cache = {}
            return _overrides_cache
        try:
            _overrides_cache = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            _overrides_cache = {}
        return _overrides_cache


def get_all_configs() -> Dict[str, Dict[str, Any]]:
    overrides = _load_overrides()
    result = {}
    for mode, defaults in DEFAULTS.items():
        merged = dict(defaults)
        if mode in overrides:
            merged.update(overrides[mode])
        result[mode] = merged
    return result


def get_mode_config(mode: str) -> Dict[str, Any]:
    all_cfg = get_all_configs()
    if mode not in all_cfg:
        raise ValueError(f"Invalid mode: {mode!r}")
    return dict(all_cfg[mode])


def update_mode_config(mode: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    global _overrides_cache
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode!r}")
    with _lock:
        overrides = _load_overrides()
        existing = dict(overrides.get(mode, {}))
        existing.update(updates)
        overrides[mode] = existing

        # M-10: atomic write — write to a sibling temp file then rename.
        # Rename is atomic on POSIX and near-atomic on Windows (Python 3.3+).
        tmp_path = _CONFIG_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
        tmp_path.replace(_CONFIG_PATH)

        # M-18: invalidate cache so the next read picks up the new values
        _overrides_cache = overrides

    return get_mode_config(mode)


def reset_all_configs() -> Dict[str, Dict[str, Any]]:
    global _overrides_cache
    with _lock:
        if _CONFIG_PATH.exists():
            _CONFIG_PATH.unlink()
        # M-18: clear cache so the next read starts fresh
        _overrides_cache = None
    return dict(DEFAULTS)
