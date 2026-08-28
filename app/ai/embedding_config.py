"""Single embedding contract shared by ingestion and retrieval.

The model and vector length are one schema: documents and student queries must
use the same values, and that length must match the Pinecone index dimension.
"""

from __future__ import annotations

import os
from typing import Any


def request_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "model": os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
    }
    raw_dimensions = os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "").strip()
    if not raw_dimensions:
        return options

    try:
        dimensions = int(raw_dimensions)
    except ValueError as exc:
        raise RuntimeError("OPENAI_EMBEDDING_DIMENSIONS must be a positive integer") from exc
    if dimensions <= 0:
        raise RuntimeError("OPENAI_EMBEDDING_DIMENSIONS must be a positive integer")
    options["dimensions"] = dimensions
    return options


def cache_namespace() -> str:
    options = request_options()
    return f"{options['model']}:{options.get('dimensions', 'default')}"
