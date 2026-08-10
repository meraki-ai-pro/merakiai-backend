"""Parent-child ("small-to-big") chunking over parsed document blocks.

Replaces the paragraph chunker, which had three defects that together made
maths unteachable:

* it split on ``"\\n\\n"`` while the parser joined with ``"\\n"``, so nothing ever
  split and a whole chapter became one chunk;
* it discarded anything under 40 words, deleting formula-dense material — the
  very passages a maths course depends on;
* it recorded only ``{mode, topic, text}``, leaving no way to cite a source.

The strategy here follows the "Parent-Child / Small-to-Big" pattern from the
spec: embed **small** children so vector similarity is precise, but hand the
model the **parent** section so it has room to reason. Short blocks are merged
into their neighbours rather than dropped, so an equation always travels with
the prose that explains it.

Parents ride along in the child's metadata rather than living in a side table:
retrieval stays a single Pinecone round-trip, which matters because this whole
workstream is about latency.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Iterable, List, Optional

from app.ai.ingestion.math_parser import EQUATION, HEADING, TABLE

logger = logging.getLogger(__name__)

Chunk = Dict[str, Any]

# A child is what gets embedded: small enough that similarity is precise.
CHILD_TARGET_CHARS = 700
CHILD_MAX_CHARS = 1200
# Below this a chunk carries too little signal to stand alone, so it is merged
# into its neighbour instead of being dropped (the old floor deleted formulas).
CHILD_MIN_CHARS = 180

# A parent is what the model reads. Capped so it fits comfortably inside
# Pinecone's 40KB per-vector metadata budget alongside everything else.
PARENT_MAX_CHARS = 6000

# Stable namespace so re-ingesting a document produces the same vector ids
# and upserts overwrite rather than duplicate.
_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")

_ATOMIC_TYPES = {EQUATION, TABLE}


def _block_text(block: Dict[str, Any]) -> str:
    return (block.get("text") or "").strip()


def _join(blocks: Iterable[Dict[str, Any]]) -> str:
    return "\n\n".join(t for t in (_block_text(b) for b in blocks) if t)


def _heading_prefix(heading_path: List[str], section_title: str) -> str:
    """Topic breadcrumb prepended to each child before embedding.

    A bare paragraph reading "Apply the rule when n is non-zero" embeds poorly;
    the same text under "Differentiation > The Power Rule" lands near queries
    about differentiation. The prefix is embedded but stripped from the text the
    model is shown, so it never leaks into an answer.
    """
    parts = [p for p in (*heading_path, section_title) if p]
    # Drop consecutive duplicates (a section title often repeats its parent).
    deduped: List[str] = []
    for part in parts:
        if not deduped or deduped[-1] != part:
            deduped.append(part)
    return " > ".join(deduped)


def _split_sections(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group flat blocks into sections, each introduced by a heading."""
    sections: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for block in blocks:
        if block.get("type") == HEADING:
            if current and current["blocks"]:
                sections.append(current)
            current = {
                "title": _block_text(block),
                "heading_path": list(block.get("heading_path") or []),
                "blocks": [],
            }
            continue

        if current is None:
            # Content before the first heading (preamble, abstract, cover page).
            current = {
                "title": "",
                "heading_path": list(block.get("heading_path") or []),
                "blocks": [],
            }
        if _block_text(block):
            current["blocks"].append(block)

    if current and current["blocks"]:
        sections.append(current)
    return sections


def _split_by_budget(
    blocks: List[Dict[str, Any]], budget: int
) -> List[List[Dict[str, Any]]]:
    """Split a block list into groups no larger than ``budget`` characters.

    Splits only at block boundaries, so an equation or a table is never cut in
    half. A single block larger than the budget becomes a group of its own
    rather than being truncated.
    """
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    size = 0

    for block in blocks:
        length = len(_block_text(block))
        if current and size + length > budget:
            groups.append(current)
            current, size = [], 0
        current.append(block)
        size += length + 2

    if current:
        groups.append(current)
    return groups


def _merge_small_groups(
    groups: List[List[Dict[str, Any]]]
) -> List[List[Dict[str, Any]]]:
    """Fold undersized groups into a neighbour instead of discarding them.

    A standalone display equation is ~30 characters. On its own it is a useless
    retrieval target; attached to the worked example around it, it is exactly
    what a student's question should match.
    """
    if len(groups) <= 1:
        return groups

    merged: List[List[Dict[str, Any]]] = []
    for group in groups:
        text_len = len(_join(group))
        is_atomic_only = all(b.get("type") in _ATOMIC_TYPES for b in group)

        if merged and (text_len < CHILD_MIN_CHARS or is_atomic_only):
            candidate = merged[-1] + group
            if len(_join(candidate)) <= CHILD_MAX_CHARS:
                merged[-1] = candidate
                continue
        merged.append(group)

    # A small leading group has no predecessor to merge into; push it forward.
    if len(merged) > 1 and len(_join(merged[0])) < CHILD_MIN_CHARS:
        if len(_join(merged[0] + merged[1])) <= CHILD_MAX_CHARS:
            merged[1] = merged[0] + merged[1]
            merged.pop(0)

    return merged


def _collect_equations(blocks: Iterable[Dict[str, Any]]) -> List[str]:
    equations: List[str] = []
    for block in blocks:
        equations.extend(block.get("equations") or [])
    return equations


def _page_range(blocks: Iterable[Dict[str, Any]]) -> tuple[Optional[int], Optional[int]]:
    pages = [b.get("page") for b in blocks if b.get("page") is not None]
    if not pages:
        return None, None
    return min(pages), max(pages)


def build_chunks(
    blocks: List[Dict[str, Any]],
    *,
    document_id: str,
    source_filename: str,
    mode: str,
    difficulty: str = "beginner",
    course_id: str = "",
) -> List[Chunk]:
    """Turn parsed blocks into embeddable child chunks carrying their parents."""
    sections = _split_sections(blocks)
    chunks: List[Chunk] = []
    chunk_index = 0

    for section in sections:
        title = section["title"]
        heading_path = section["heading_path"]
        breadcrumb = _heading_prefix(heading_path, title)

        for parent_blocks in _split_by_budget(section["blocks"], PARENT_MAX_CHARS):
            parent_text = _join(parent_blocks)
            if not parent_text:
                continue

            parent_id = str(
                uuid.uuid5(_ID_NAMESPACE, f"{document_id}:parent:{chunk_index}")
            )
            parent_start, parent_end = _page_range(parent_blocks)

            child_groups = _merge_small_groups(
                _split_by_budget(parent_blocks, CHILD_TARGET_CHARS)
            )

            for group in child_groups:
                child_text = _join(group)
                if not child_text:
                    continue

                page_start, page_end = _page_range(group)
                equations = _collect_equations(group)
                chunk_id = str(
                    uuid.uuid5(_ID_NAMESPACE, f"{document_id}:child:{chunk_index}")
                )

                chunks.append({
                    "id": chunk_id,
                    # What gets embedded — breadcrumb included so the vector
                    # knows its topic.
                    "embed_text": f"{breadcrumb}\n\n{child_text}" if breadcrumb else child_text,
                    # What gets shown/cited.
                    "text": child_text,
                    "parent_id": parent_id,
                    "parent_text": parent_text,
                    "heading_path": heading_path,
                    "section_title": title,
                    "breadcrumb": breadcrumb,
                    "page": page_start if page_start is not None else parent_start,
                    "page_end": page_end if page_end is not None else parent_end,
                    "has_math": any(b.get("has_math") for b in group),
                    "equations": equations,
                    "needs_math_ocr": any(b.get("needs_math_ocr") for b in group),
                    "chunk_index": chunk_index,
                    "mode": mode,
                    "topic": title or (heading_path[-1] if heading_path else ""),
                    "difficulty": difficulty,
                    "document_id": document_id,
                    "source_filename": source_filename,
                    "course_id": course_id,
                })
                chunk_index += 1

    logger.info(
        "Chunked %s: %d chunks (%d with maths) from %d blocks",
        source_filename,
        len(chunks),
        sum(1 for c in chunks if c["has_math"]),
        len(blocks),
    )
    return chunks


# Pinecone accepts only str, number, bool, or list[str] as metadata values, and
# rejects nulls outright.
_METADATA_FIELDS = (
    "text", "parent_id", "parent_text", "section_title", "breadcrumb",
    "topic", "mode", "difficulty", "document_id", "source_filename",
    "course_id", "has_math", "chunk_index", "page", "page_end",
)


def to_pinecone_metadata(chunk: Chunk) -> Dict[str, Any]:
    """Project a chunk onto Pinecone-safe metadata, dropping empty values.

    Every field a citation needs — document, filename, page, heading path — is
    carried here. The previous pipeline stored only ``{mode, topic, text}``,
    which is why inline citations were impossible to build.
    """
    metadata: Dict[str, Any] = {}

    for field in _METADATA_FIELDS:
        value = chunk.get(field)
        if value is None or value == "":
            continue
        metadata[field] = value

    heading_path = [h for h in (chunk.get("heading_path") or []) if h]
    if heading_path:
        metadata["heading_path"] = heading_path

    equations = [e for e in (chunk.get("equations") or []) if e]
    if equations:
        # Keep the metadata payload bounded; the full maths is already in `text`.
        metadata["equations"] = equations[:20]

    return metadata
