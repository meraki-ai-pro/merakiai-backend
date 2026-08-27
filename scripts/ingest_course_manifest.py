"""Validate and ingest the reviewed Level 100 course manifest.

The command is read-only unless ``--execute`` is supplied. Live uploads remain
drafts so a lecturer can run sample questions before publishing. Froth
Flotation cleanup is a separate, exact-match operation which is permitted only
after every selected document is ready and its target namespaces are populated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import UploadFile

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.ai.ingestion.math_parser import parse_blocks  # noqa: E402
from app.ai.ingestion.namespaces import namespace_for  # noqa: E402
from app.ai.ingestion.service import ingest_document  # noqa: E402
from app.config import load_env  # noqa: E402
from app.db.supabase import get_supabase  # noqa: E402


DEFAULT_MANIFEST = BACKEND / "config" / "course_ingestion_manifest.json"
COURSE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
ALLOWED_SUFFIXES = {".docx", ".pptx", ".pdf"}
ALLOWED_MODES = {"learn", "review", "application"}
ALLOWED_DIFFICULTIES = {"basic", "intermediate", "advanced"}
ALLOWED_FORMATS = {"mcq", "fill_blank", "short_answer"}
TERMINAL_STATUSES = {"ready", "failed", "no_content"}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--course", action="append", dest="courses")
    parser.add_argument(
        "--parse",
        action="store_true",
        help="Parse every selected source file locally as an additional preflight.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create missing courses and enqueue selected documents as drafts.",
    )
    parser.add_argument("--wait-minutes", type=int, default=45)
    parser.add_argument(
        "--remove-froth-after-verify",
        action="store_true",
        help="Delete only exact Froth Flotation namespaces after full corpus verification.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="Required exact confirmation phrase for Froth namespace deletion.",
    )
    return parser.parse_args()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid manifest JSON at line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Manifest root must be an object")
    return payload


def resolve_knowledge_root(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    require_exists: bool = True,
) -> Path:
    configured = str(manifest.get("knowledge_root") or "").strip()
    if not configured:
        raise ValueError("knowledge_root is required")
    root = (manifest_path.parent / configured).resolve()
    if require_exists and not root.is_dir():
        raise ValueError(f"Knowledge root does not exist: {root}")
    return root


def selected_courses(
    manifest: dict[str, Any], requested: list[str] | None
) -> list[dict[str, Any]]:
    courses = manifest.get("courses")
    if not isinstance(courses, list) or not courses:
        raise ValueError("Manifest must contain at least one course")
    if not requested:
        return courses
    requested_set = set(requested)
    found = [course for course in courses if course.get("id") in requested_set]
    missing = requested_set - {str(course.get("id")) for course in found}
    if missing:
        raise ValueError(f"Unknown course id(s): {', '.join(sorted(missing))}")
    return found


def _source_path(root: Path, relative: str) -> Path:
    source = (root / relative).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Source escapes knowledge_root: {relative}") from exc
    return source


def validate_manifest(
    manifest: dict[str, Any],
    manifest_path: Path,
    requested: list[str] | None = None,
    *,
    require_source_files: bool = True,
) -> tuple[Path, list[dict[str, Any]]]:
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported schema_version; expected 1")
    lecturer = manifest.get("lecturer") or {}
    if not str(lecturer.get("email") or "").strip():
        raise ValueError("lecturer.email is required")

    root = resolve_knowledge_root(
        manifest_path,
        manifest,
        require_exists=require_source_files,
    )
    courses = selected_courses(manifest, requested)
    seen_course_ids: set[str] = set()
    seen_sources: set[str] = set()

    for course in courses:
        course_id = str(course.get("id") or "")
        if not COURSE_ID_RE.fullmatch(course_id):
            raise ValueError(f"Invalid course id: {course_id!r}")
        if course_id in seen_course_ids:
            raise ValueError(f"Duplicate course id: {course_id}")
        seen_course_ids.add(course_id)
        if course.get("academic_level") != "level_100":
            raise ValueError(f"{course_id}: academic_level must be level_100")

        documents = course.get("documents")
        if not isinstance(documents, list) or not documents:
            raise ValueError(f"{course_id}: documents must be a non-empty list")

        for document in documents:
            relative = str(document.get("path") or "")
            source = _source_path(root, relative)
            if require_source_files and not source.is_file():
                raise ValueError(f"Missing source file: {relative}")
            if source.suffix.lower() not in ALLOWED_SUFFIXES:
                raise ValueError(f"Unsupported source type: {relative}")
            source_key = str(source).casefold()
            if source_key in seen_sources:
                raise ValueError(f"Source appears more than once: {relative}")
            seen_sources.add(source_key)

            modes = document.get("target_modes")
            if not isinstance(modes, list) or not modes or not set(modes) <= ALLOWED_MODES:
                raise ValueError(f"{relative}: invalid target_modes")
            if document.get("default_mode") not in modes:
                raise ValueError(f"{relative}: default_mode must be in target_modes")
            if document.get("difficulty") not in ALLOWED_DIFFICULTIES:
                raise ValueError(f"{relative}: invalid difficulty")
            formats = document.get("question_formats")
            if formats is not None:
                if not isinstance(formats, list) or not set(formats) <= ALLOWED_FORMATS:
                    raise ValueError(f"{relative}: invalid question_formats")
                if formats and "review" not in modes:
                    raise ValueError(f"{relative}: question_formats require Review mode")

            expected_default = {
                "knowledge": "learn",
                "assessment": "review",
                "practice": "application",
            }.get(document.get("doc_type"))
            if expected_default is None:
                raise ValueError(f"{relative}: invalid doc_type")

    cleanup = manifest.get("pinecone_cleanup") or {}
    try:
        re.compile(str(cleanup.get("namespace_pattern") or ""))
    except re.error as exc:
        raise ValueError(f"Invalid Pinecone namespace pattern: {exc}") from exc
    if not str(cleanup.get("confirmation_phrase") or ""):
        raise ValueError("pinecone_cleanup.confirmation_phrase is required")
    return root, courses


def parse_preflight(root: Path, courses: list[dict[str, Any]]) -> None:
    for course in courses:
        for document in course["documents"]:
            if not document["ingest"]:
                continue
            source = _source_path(root, document["path"])
            blocks = parse_blocks(source.read_bytes(), source.name)
            text_blocks = [block for block in blocks if str(block.get("text") or "").strip()]
            if not text_blocks:
                raise ValueError(f"Parser found no content in {document['path']}")
            print(f"PARSE OK  {document['path']}  blocks={len(text_blocks)}")


def _lecturer_profile(supabase, email: str) -> dict[str, Any]:
    rows = (
        supabase.table("users")
        .select("id,email,role")
        .eq("email", email.strip().lower())
        .limit(2)
        .execute()
        .data
        or []
    )
    if len(rows) != 1:
        raise RuntimeError(f"Expected one public user profile for {email}; found {len(rows)}")
    if rows[0].get("role") != "lecturer":
        raise RuntimeError(f"{email} exists but is not a lecturer")
    return rows[0]


def ensure_courses(supabase, courses: list[dict[str, Any]], owner_id: str) -> None:
    for course in courses:
        course_id = course["id"]
        existing = (
            supabase.table("courses")
            .select("id,owner_id,academic_level")
            .eq("id", course_id)
            .execute()
            .data
            or []
        )
        if existing:
            if existing[0].get("owner_id") != owner_id:
                raise RuntimeError(
                    f"Course {course_id} already belongs to another user; ownership was not changed"
                )
            if existing[0].get("academic_level") != "level_100":
                raise RuntimeError(f"Course {course_id} exists at a different academic level")
            print(f"COURSE OK  {course_id}")
            continue

        row = {
            key: course[key]
            for key in (
                "id",
                "name",
                "description",
                "domain_topics",
                "academic_level",
                "practice_mode_enabled",
                "subject",
            )
        }
        row["owner_id"] = owner_id
        created = supabase.table("courses").insert(row).execute().data or []
        if not created:
            raise RuntimeError(f"Failed to create course {course_id}")
        print(f"COURSE CREATED  {course_id}")


def _existing_document(supabase, course_id: str, filename: str) -> dict[str, Any] | None:
    rows = (
        supabase.table("documents")
        .select("id,source_filename,status,target_modes,is_published")
        .eq("course_id", course_id)
        .eq("source_filename", filename)
        .execute()
        .data
        or []
    )
    if len(rows) > 1:
        raise RuntimeError(
            f"Duplicate document rows already exist for {course_id}/{filename}; resolve them first"
        )
    return rows[0] if rows else None


async def enqueue_documents(
    supabase, root: Path, courses: list[dict[str, Any]], owner_id: str
) -> list[str]:
    document_ids: list[str] = []
    for course in courses:
        for document in course["documents"]:
            if not document["ingest"]:
                continue
            source = _source_path(root, document["path"])
            existing = _existing_document(supabase, course["id"], source.name)
            if existing:
                if existing.get("status") != "ready":
                    raise RuntimeError(
                        f"Existing document {source.name} is {existing.get('status')}; "
                        "inspect or remove it before retrying"
                    )
                if sorted(existing.get("target_modes") or []) != sorted(document["target_modes"]):
                    raise RuntimeError(f"Existing document {source.name} has different target modes")
                print(f"DOCUMENT READY  {course['id']}/{source.name}")
                document_ids.append(existing["id"])
                continue

            with source.open("rb") as handle:
                upload = UploadFile(filename=source.name, file=handle)
                result = await ingest_document(
                    file=upload,
                    course_id=course["id"],
                    doc_type=document["doc_type"],
                    default_mode=document["default_mode"],
                    difficulty=document["difficulty"],
                    version="1",
                    user_id=owner_id,
                    target_modes=document["target_modes"],
                    is_published=False,
                    topic=document["topic"],
                    question_formats=document["question_formats"],
                )
            document_ids.append(result["document_id"])
            print(f"DOCUMENT QUEUED  {course['id']}/{source.name}")
    return document_ids


def wait_for_documents(supabase, document_ids: list[str], wait_minutes: int) -> None:
    deadline = time.monotonic() + max(1, wait_minutes) * 60
    pending = set(document_ids)
    last_statuses: dict[str, str] = {}
    while pending and time.monotonic() < deadline:
        rows = (
            supabase.table("documents")
            .select("id,source_filename,status,total_chunks")
            .in_("id", list(pending))
            .execute()
            .data
            or []
        )
        for row in rows:
            status = str(row.get("status") or "unknown")
            if last_statuses.get(row["id"]) != status:
                print(f"STATUS  {row['source_filename']}  {status}")
                last_statuses[row["id"]] = status
            if status in TERMINAL_STATUSES:
                pending.discard(row["id"])
        if pending:
            time.sleep(10)
    if pending:
        raise RuntimeError(f"Timed out waiting for {len(pending)} document(s)")


def _namespace_counts(index) -> dict[str, int]:
    stats = index.describe_index_stats()
    namespaces = getattr(stats, "namespaces", None)
    if namespaces is None and isinstance(stats, dict):
        namespaces = stats.get("namespaces")
    result: dict[str, int] = {}
    for name, summary in (namespaces or {}).items():
        count = getattr(summary, "vector_count", None)
        if count is None and isinstance(summary, dict):
            count = summary.get("vector_count", 0)
        result[str(name)] = int(count or 0)
    return result


def verify_corpus(supabase, courses: list[dict[str, Any]]) -> dict[str, int]:
    required_namespaces: set[str] = set()
    for course in courses:
        for document in course["documents"]:
            if not document["ingest"]:
                continue
            filename = Path(document["path"]).name
            existing = _existing_document(supabase, course["id"], filename)
            if not existing or existing.get("status") != "ready":
                raise RuntimeError(f"Corpus verification failed: {course['id']}/{filename} is not ready")
            for mode in document["target_modes"]:
                namespace = namespace_for(course["id"], mode)
                required_namespaces.add(namespace)
                rows = (
                    supabase.table("document_chunks")
                    .select("id")
                    .eq("document_id", existing["id"])
                    .eq("pinecone_namespace", namespace)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                if not rows:
                    raise RuntimeError(f"No chunk metadata for {filename} in {namespace}")

    from app.ai.ingestion.pinecone import _get_index

    index = _get_index()
    counts = _namespace_counts(index)
    empty = sorted(namespace for namespace in required_namespaces if counts.get(namespace, 0) < 1)
    if empty:
        raise RuntimeError(f"Required Pinecone namespace(s) are empty: {', '.join(empty)}")
    for namespace in sorted(required_namespaces):
        print(f"NAMESPACE VERIFIED  {namespace}  vectors={counts[namespace]}")
    return counts


def remove_froth_namespaces(
    manifest: dict[str, Any], counts: dict[str, int], confirmation: str
) -> None:
    cleanup = manifest["pinecone_cleanup"]
    phrase = cleanup["confirmation_phrase"]
    if confirmation != phrase:
        raise RuntimeError(f"Froth cleanup requires --confirm {phrase}")
    pattern = re.compile(cleanup["namespace_pattern"])
    targets = sorted(namespace for namespace in counts if pattern.fullmatch(namespace))
    if not targets:
        print("No exact Froth Flotation namespaces were found; nothing deleted")
        return

    from app.ai.ingestion.pinecone import _get_index

    index = _get_index()
    for namespace in targets:
        index.delete(delete_all=True, namespace=namespace)
        print(f"FROTH NAMESPACE DELETED  {namespace}")


def print_plan(root: Path, courses: list[dict[str, Any]]) -> None:
    print(f"Knowledge root: {root}")
    for course in courses:
        selected = [document for document in course["documents"] if document["ingest"]]
        excluded = [document for document in course["documents"] if not document["ingest"]]
        print(
            f"COURSE  {course['id']}  {course['academic_level']}  "
            f"selected={len(selected)} excluded={len(excluded)}"
        )
        for document in selected:
            print(
                f"  INGEST  {document['path']}  modes={','.join(document['target_modes'])}  "
                f"difficulty={document['difficulty']}"
            )
        for document in excluded:
            print(f"  EXCLUDE {document['path']}  reason={document['rationale']}")


def main() -> int:
    args = arguments()
    if args.remove_froth_after_verify and args.courses:
        raise SystemExit(
            "Froth cleanup requires verification of the complete Statistics and Calculus manifest; "
            "do not combine it with --course"
        )
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    root, courses = validate_manifest(manifest, manifest_path, args.courses)
    print_plan(root, courses)
    if args.parse or args.execute:
        parse_preflight(root, courses)
    if not args.execute:
        if args.remove_froth_after_verify:
            raise SystemExit("--remove-froth-after-verify also requires --execute")
        print("DRY RUN COMPLETE: no external state changed")
        return 0

    load_env()
    supabase = get_supabase()
    lecturer = _lecturer_profile(supabase, manifest["lecturer"]["email"])
    ensure_courses(supabase, courses, lecturer["id"])
    document_ids = asyncio.run(enqueue_documents(supabase, root, courses, lecturer["id"]))
    wait_for_documents(supabase, document_ids, args.wait_minutes)
    namespace_counts = verify_corpus(supabase, courses)
    if args.remove_froth_after_verify:
        remove_froth_namespaces(manifest, namespace_counts, args.confirm)
    print("LIVE INGESTION VERIFIED: documents remain drafts pending semantic smoke tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
