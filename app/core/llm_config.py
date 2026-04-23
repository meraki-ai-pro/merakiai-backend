from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "llm_config.json"
_lock = threading.Lock()

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
}


def _load_overrides() -> Dict[str, Any]:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode!r}")
    with _lock:
        overrides = _load_overrides()
        existing = dict(overrides.get(mode, {}))
        existing.update(updates)
        overrides[mode] = existing
        _CONFIG_PATH.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
    return get_mode_config(mode)


def reset_all_configs() -> Dict[str, Dict[str, Any]]:
    with _lock:
        if _CONFIG_PATH.exists():
            _CONFIG_PATH.unlink()
    return dict(DEFAULTS)
