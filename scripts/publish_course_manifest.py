"""Publish the verified Level 100 corpus and test live visibility through the API."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import load_env  # noqa: E402
from app.db.supabase import get_supabase  # noqa: E402
from scripts.ingest_course_manifest import (  # noqa: E402
    DEFAULT_MANIFEST,
    _lecturer_profile,
    _temporary_api_token,
    load_manifest,
    validate_manifest,
)
from scripts.smoke_test_course_namespaces import QUERIES  # noqa: E402


CONFIRMATION = "PUBLISH LEVEL 100 CORPUS"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="https://api.merakiai.online")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser.parse_args()


def verified_documents(
    supabase, courses: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    verified: list[tuple[str, dict[str, Any]]] = []
    for course in courses:
        for document in course["documents"]:
            if not document["ingest"]:
                continue
            filename = Path(document["path"]).name
            rows = (
                supabase.table("documents")
                .select(
                    "id,status,total_chunks,is_published,deleted_at,target_modes,source_filename"
                )
                .eq("course_id", course["id"])
                .eq("source_filename", filename)
                .execute()
                .data
                or []
            )
            if len(rows) != 1:
                raise RuntimeError(
                    f"Expected one document for {course['id']}/{filename}; found {len(rows)}"
                )
            row = rows[0]
            safe = (
                row.get("status") == "ready"
                and int(row.get("total_chunks") or 0) > 0
                and not row.get("deleted_at")
                and sorted(row.get("target_modes") or [])
                == sorted(document["target_modes"])
            )
            if not safe:
                raise RuntimeError(
                    f"Refusing to publish unverified document {course['id']}/{filename}"
                )
            verified.append((course["id"], row))
    return verified


def main() -> int:
    args = arguments()
    load_env()
    manifest = load_manifest(DEFAULT_MANIFEST)
    _, courses = validate_manifest(
        manifest, DEFAULT_MANIFEST, require_source_files=False
    )
    supabase = get_supabase()
    lecturer = _lecturer_profile(supabase, manifest["lecturer"]["email"])
    documents = verified_documents(supabase, courses)
    print(f"PUBLISH PREFLIGHT VERIFIED  documents={len(documents)}")
    if not args.execute:
        print(f"DRY RUN: rerun with --execute --confirm \"{CONFIRMATION}\"")
        return 0
    if args.confirm != CONFIRMATION:
        raise RuntimeError(f"Publishing requires --confirm \"{CONFIRMATION}\"")

    token = _temporary_api_token(lecturer)
    base_url = args.api_url.rstrip("/")
    if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise RuntimeError("--api-url must use HTTPS (except localhost development)")
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
        follow_redirects=False,
    ) as client:
        for course_id, document in documents:
            if document.get("is_published"):
                print(f"ALREADY PUBLISHED  {course_id}/{document['source_filename']}")
                continue
            response = client.patch(
                f"/lecturer/courses/{course_id}/knowledge/{document['id']}",
                json={"is_published": True},
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Publish failed for {course_id}/{document['source_filename']}: "
                    f"HTTP {response.status_code} {response.text[:500]}"
                )
            print(f"PUBLISHED  {course_id}/{document['source_filename']}")

        for (course_id, mode), question in QUERIES.items():
            response = client.post(
                f"/lecturer/courses/{course_id}/knowledge/test-query",
                json={"question": question, "mode": mode},
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Live retrieval failed for {course_id}/{mode}: HTTP {response.status_code}"
                )
            payload = response.json()
            if int(payload.get("count") or 0) < 1:
                raise RuntimeError(f"Live retrieval returned no results for {course_id}/{mode}")
            sources = sorted(
                {
                    str(result.get("source_filename"))
                    for result in payload.get("results", [])
                    if result.get("source_filename")
                }
            )
            print(
                f"LIVE RETRIEVAL OK  {course_id}/{mode}  "
                f"count={payload['count']} sources={','.join(sources)}"
            )

    print("CORPUS PUBLISHED AND LIVE RETRIEVAL VERIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
