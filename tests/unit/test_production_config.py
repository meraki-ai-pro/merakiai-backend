from __future__ import annotations

import pytest

from app import config


def test_production_validation_names_all_missing_settings(monkeypatch):
    for name in config._REQUIRED_PRODUCTION_SETTINGS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        config._validate_production_settings()

    message = str(exc_info.value)
    assert "SUPABASE_SERVICE_ROLE_KEY" in message
    assert "ALLOWED_HOSTS" in message
    assert "PINECONE_INDEX" in message


def test_production_validation_accepts_complete_contract(monkeypatch):
    for name in config._REQUIRED_PRODUCTION_SETTINGS:
        monkeypatch.setenv(name, "configured")

    config._validate_production_settings()
