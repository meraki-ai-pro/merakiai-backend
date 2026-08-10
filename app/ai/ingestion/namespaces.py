"""Versioned Pinecone namespace naming.

The maths-aware parser and parent-child chunker produce fundamentally different
vectors from the old pipeline: different chunk boundaries, equations that
previously did not exist, and metadata that citations depend on. Mixing the two
in one namespace would give a course a mixture of citable and uncitable
material with inconsistent granularity.

So each pipeline generation writes to its own namespace. ``v1`` keeps the legacy
un-suffixed name so existing vectors stay exactly where they are, untouched and
still queryable, while re-ingested documents land in ``v2``.

Retrieval reads the current version and falls back to the legacy one when a
course has not been re-ingested yet, which means the switchover needs no
flag-flipping and no downtime.
"""

from __future__ import annotations

import os

# Namespace generation written by new ingestions.
CURRENT_VERSION = "v2"
LEGACY_VERSION = "v1"


def ingest_version() -> str:
    return os.getenv("INGEST_PIPELINE_VERSION", CURRENT_VERSION).strip().lower()


def namespace_for(course_id: str, mode: str, version: str | None = None) -> str:
    """Build the namespace for a course/mode pair.

    ``v1`` is deliberately un-suffixed: that is the name the existing vectors
    already live under, and renaming them would mean re-uploading everything.
    """
    course_id = (course_id or "").strip()
    mode = (mode or "learn").strip().lower()
    version = (version or ingest_version()).strip().lower()

    base = f"{course_id}-{mode}"
    if version in (LEGACY_VERSION, "legacy", ""):
        return base
    return f"{base}-{version}"


def search_namespaces(course_id: str, mode: str) -> list[str]:
    """Namespaces to search, newest generation first.

    Returning both lets a course that has been only partly re-ingested keep
    answering from its legacy vectors instead of going silent.
    """
    forced = os.getenv("RAG_NAMESPACE_VERSION", "").strip().lower()
    if forced:
        return [namespace_for(course_id, mode, forced)]

    current = namespace_for(course_id, mode, CURRENT_VERSION)
    legacy = namespace_for(course_id, mode, LEGACY_VERSION)
    return [current, legacy] if current != legacy else [current]
