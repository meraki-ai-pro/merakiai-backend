"""Run read-only semantic smoke queries against the production course namespaces."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.ai.ingestion.embedder import embed_chunks  # noqa: E402
from app.ai.ingestion.namespaces import namespace_for  # noqa: E402
from app.ai.ingestion.pinecone import _get_index  # noqa: E402
from app.config import load_env  # noqa: E402
from scripts.ingest_course_manifest import (  # noqa: E402
    DEFAULT_MANIFEST,
    load_manifest,
    validate_manifest,
)


QUERIES = {
    ("calculus-100", "learn"): "Define a derivative using limits and explain its meaning.",
    ("calculus-100", "review"): "Differentiate a composite function using the chain rule.",
    ("calculus-100", "application"): "Use derivatives to find and classify a maximum or minimum.",
    ("statistics-100", "learn"): "Explain Bayes theorem and conditional probability.",
    ("statistics-100", "review"): "Calculate and interpret a confidence interval for a population mean.",
    ("statistics-100", "application"): "Choose a probability model for a real-world random experiment.",
}


def _matches(response):
    matches = getattr(response, "matches", None)
    if matches is None and isinstance(response, dict):
        matches = response.get("matches")
    return matches or []


def _metadata(match):
    value = getattr(match, "metadata", None)
    if value is None and isinstance(match, dict):
        value = match.get("metadata")
    return value or {}


def _score(match) -> float:
    value = getattr(match, "score", None)
    if value is None and isinstance(match, dict):
        value = match.get("score")
    return float(value or 0.0)


async def main() -> int:
    load_env()
    manifest = load_manifest(DEFAULT_MANIFEST)
    _, courses = validate_manifest(
        manifest, DEFAULT_MANIFEST, require_source_files=False
    )
    expected_sources = {
        course["id"]: {
            Path(document["path"]).name
            for document in course["documents"]
            if document["ingest"]
        }
        for course in courses
    }

    keys = list(QUERIES)
    vectors = await embed_chunks([QUERIES[key] for key in keys])
    index = _get_index()
    failures: list[str] = []
    for (course_id, mode), vector in zip(keys, vectors, strict=True):
        namespace = namespace_for(course_id, mode)
        response = index.query(
            namespace=namespace,
            vector=vector,
            top_k=3,
            include_metadata=True,
        )
        matches = _matches(response)
        valid = [
            match
            for match in matches
            if _metadata(match).get("source_filename") in expected_sources[course_id]
        ]
        if not valid:
            failures.append(f"{namespace}: no approved source returned")
            continue
        citations = ", ".join(
            f"{_metadata(match).get('source_filename')} ({_score(match):.3f})"
            for match in valid
        )
        print(f"SMOKE OK  {namespace}  {citations}")

    if failures:
        raise RuntimeError("; ".join(failures))
    print("SEMANTIC SMOKE VERIFIED: all six production namespaces returned approved sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
