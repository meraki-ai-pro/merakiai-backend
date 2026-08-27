from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.ingest_course_manifest import (
    DEFAULT_MANIFEST,
    _source_path,
    load_manifest,
    validate_manifest,
)


def test_production_manifest_is_complete_and_level_100():
    manifest = load_manifest(DEFAULT_MANIFEST)
    root, courses = validate_manifest(
        manifest,
        DEFAULT_MANIFEST,
        require_source_files=False,
    )

    assert root.name == "Knowledge Files"
    assert {course["id"] for course in courses} == {"calculus-100", "statistics-100"}
    assert all(course["academic_level"] == "level_100" for course in courses)

    selected = [
        document
        for course in courses
        for document in course["documents"]
        if document["ingest"]
    ]
    excluded = [
        document
        for course in courses
        for document in course["documents"]
        if not document["ingest"]
    ]
    assert len(selected) == 27
    assert len(excluded) == 8
    assert all(document["rationale"].strip() for document in selected + excluded)


def test_assessment_sources_with_answers_do_not_feed_application_mode():
    manifest = load_manifest(DEFAULT_MANIFEST)
    _, courses = validate_manifest(
        manifest,
        DEFAULT_MANIFEST,
        require_source_files=False,
    )
    answer_bearing = {
        "Calculus/Quiz01(20230617)Wds.pptxnew.pptx",
        "Statistics/Midsem(20250322).pptx",
        "Statistics/Midsem 2_04042025_hdrc2314.pptx",
        "Statistics/hdrc2314Semester_(202604)Marking scheme.docx",
        "Statistics/Tutorial Questions and Answers.docx",
    }
    documents = {
        document["path"]: document
        for course in courses
        for document in course["documents"]
    }

    assert answer_bearing <= documents.keys()
    assert all("application" not in documents[path]["target_modes"] for path in answer_bearing)


def test_cleanup_pattern_matches_only_froth_course_namespaces():
    manifest = load_manifest(DEFAULT_MANIFEST)
    pattern = re.compile(manifest["pinecone_cleanup"]["namespace_pattern"])

    assert pattern.fullmatch("froth-flotation-learn")
    assert pattern.fullmatch("froth-flotation-review-v2")
    assert not pattern.fullmatch("statistics-100-learn-v2")
    assert not pattern.fullmatch("my-froth-flotation-learn-v2")
    assert not pattern.fullmatch("froth-flotation-extra-learn-v2")


def test_source_path_cannot_escape_knowledge_root(tmp_path: Path):
    root = tmp_path / "knowledge"
    root.mkdir()
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"test")

    with pytest.raises(ValueError, match="escapes knowledge_root"):
        _source_path(root, "../outside.docx")
