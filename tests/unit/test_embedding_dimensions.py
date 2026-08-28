from __future__ import annotations

import pytest

from app.ai import embedding_config
from app.ai.ingestion import embedder


def test_embedding_options_include_configured_dimension(monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("OPENAI_EMBEDDING_DIMENSIONS", "1024")

    assert embedding_config.request_options() == {
        "model": "text-embedding-3-large",
        "dimensions": 1024,
    }


@pytest.mark.parametrize("value", ["0", "-1", "large"])
def test_embedding_dimension_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("OPENAI_EMBEDDING_DIMENSIONS", value)

    with pytest.raises(RuntimeError, match="positive integer"):
        embedding_config.request_options()


@pytest.mark.asyncio
async def test_ingestion_passes_the_dimension_to_openai(monkeypatch):
    calls = []

    class Embeddings:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return type("Response", (), {"data": [type("Row", (), {"embedding": [0.1]})()]})()

    fake_client = type("Client", (), {"embeddings": Embeddings()})()
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("OPENAI_EMBEDDING_DIMENSIONS", "1024")
    monkeypatch.setattr(embedder, "_get_client", lambda: fake_client)

    assert await embedder.embed_chunks(["calculus"]) == [[0.1]]
    assert calls == [{
        "input": ["calculus"],
        "model": "text-embedding-3-large",
        "dimensions": 1024,
    }]


def test_cache_namespace_changes_with_embedding_schema(monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("OPENAI_EMBEDDING_DIMENSIONS", "1024")
    one = embedding_config.cache_namespace()
    monkeypatch.setenv("OPENAI_EMBEDDING_DIMENSIONS", "3072")
    two = embedding_config.cache_namespace()

    assert one != two
