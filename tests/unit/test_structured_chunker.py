"""Parent-child chunking.

The chunker this replaces split on ``"\\n\\n"`` while the parser joined with
``"\\n"`` — so it never split at all — and dropped anything under 40 words,
which quietly deleted the formula-dense passages a maths course is made of.
"""

import pytest

from app.ai.ingestion.math_parser import EQUATION, HEADING, PARAGRAPH, TABLE
from app.ai.ingestion.structured_chunker import (
    CHILD_MAX_CHARS,
    PARENT_MAX_CHARS,
    build_chunks,
    to_pinecone_metadata,
)


def block(kind, text, *, heading_path=(), page=None, equations=(), level=None):
    return {
        "type": kind,
        "text": text,
        "level": level,
        "heading_path": list(heading_path),
        "page": page,
        "equations": list(equations),
        "has_math": bool(equations),
        "index": 0,
    }


def chunk_all(blocks, **kwargs):
    kwargs.setdefault("document_id", "doc-1")
    kwargs.setdefault("source_filename", "notes.docx")
    kwargs.setdefault("mode", "learn")
    return build_chunks(blocks, **kwargs)


PROSE = (
    "A derivative measures the instantaneous rate of change of a function "
    "with respect to its input, and is defined as the limit of the difference "
    "quotient as the increment tends to zero. "
)


class TestNothingIsDropped:
    def test_equation_only_section_survives(self):
        """The old 40-word floor deleted these outright."""
        blocks = [
            block(HEADING, "Key Identity", level=1),
            block(EQUATION, r"$$\frac{d}{dx}\sin x = \cos x$$",
                  heading_path=["Key Identity"], equations=[r"\frac{d}{dx}\sin x = \cos x"]),
        ]
        chunks = chunk_all(blocks)
        assert len(chunks) == 1
        assert r"\cos x" in chunks[0]["text"]

    def test_short_paragraph_survives(self):
        chunks = chunk_all([block(PARAGRAPH, "Let f be continuous.")])
        assert len(chunks) == 1

    def test_every_block_appears_in_some_chunk(self):
        blocks = [
            block(HEADING, "Limits", level=1),
            block(PARAGRAPH, PROSE, heading_path=["Limits"]),
            block(EQUATION, "$$L = 2$$", heading_path=["Limits"], equations=["L = 2"]),
            block(PARAGRAPH, "Therefore the limit exists.", heading_path=["Limits"]),
        ]
        combined = " ".join(c["text"] for c in chunk_all(blocks))
        assert "instantaneous rate of change" in combined
        assert "L = 2" in combined
        assert "Therefore the limit exists." in combined


class TestAtomicBlocks:
    def test_equation_is_never_split_across_chunks(self):
        equation = r"\int_0^1 x^2\,dx = \frac{1}{3}"
        blocks = [
            block(PARAGRAPH, PROSE * 4),
            block(EQUATION, f"$${equation}$$", equations=[equation]),
            block(PARAGRAPH, PROSE * 4),
        ]
        chunks = chunk_all(blocks)
        holders = [c for c in chunks if equation in c["text"]]
        assert len(holders) >= 1
        # The equation appears whole wherever it appears.
        for c in holders:
            assert f"$${equation}$$" in c["text"]

    def test_table_stays_in_one_chunk(self):
        table = "\n".join(
            ["| Function | Derivative |", "| --- | --- |"]
            + [f"| f{i} | g{i} |" for i in range(40)]
        )
        chunks = chunk_all([block(TABLE, table)])
        holders = [c for c in chunks if "| f39 | g39 |" in c["text"]]
        assert len(holders) == 1
        assert "| Function | Derivative |" in holders[0]["text"]


class TestParentChild:
    @pytest.fixture
    def chunks(self):
        blocks = [block(HEADING, "Integration", level=1)] + [
            block(PARAGRAPH, PROSE * 2, heading_path=["Integration"]) for _ in range(6)
        ]
        return chunk_all(blocks)

    def test_children_are_smaller_than_their_parent(self, chunks):
        assert len(chunks) > 1
        for c in chunks:
            assert len(c["text"]) <= len(c["parent_text"])

    def test_children_share_one_parent_within_a_section(self, chunks):
        assert len({c["parent_id"] for c in chunks}) == 1

    def test_child_text_is_contained_in_parent_text(self, chunks):
        for c in chunks:
            assert c["text"] in c["parent_text"]

    def test_parent_respects_its_size_cap(self):
        blocks = [block(PARAGRAPH, PROSE * 3) for _ in range(40)]
        for c in chunk_all(blocks):
            assert len(c["parent_text"]) <= PARENT_MAX_CHARS + CHILD_MAX_CHARS

    def test_children_stay_near_the_target_size(self, chunks):
        for c in chunks:
            assert len(c["text"]) <= CHILD_MAX_CHARS


class TestBreadcrumb:
    @pytest.fixture
    def chunk(self):
        blocks = [
            block(HEADING, "The Power Rule", heading_path=["Differentiation"], level=2),
            block(PARAGRAPH, PROSE, heading_path=["Differentiation", "The Power Rule"]),
        ]
        return chunk_all(blocks)[0]

    def test_embed_text_carries_the_topic(self, chunk):
        """"Apply the rule when n is non-zero" embeds poorly without its topic."""
        assert chunk["embed_text"].startswith("Differentiation > The Power Rule")

    def test_displayed_text_has_no_breadcrumb(self, chunk):
        """The breadcrumb must not leak into what the model quotes back."""
        assert not chunk["text"].startswith("Differentiation >")

    def test_breadcrumb_does_not_repeat_a_title(self):
        blocks = [
            block(HEADING, "Limits", heading_path=["Limits"], level=1),
            block(PARAGRAPH, PROSE, heading_path=["Limits"]),
        ]
        assert chunk_all(blocks)[0]["breadcrumb"] == "Limits"


class TestProvenance:
    def test_citation_fields_are_populated(self):
        blocks = [
            block(HEADING, "Normal Distribution", level=1),
            block(PARAGRAPH, PROSE, heading_path=["Normal Distribution"], page=7),
        ]
        c = chunk_all(blocks, document_id="d9", source_filename="stats.pdf",
                      course_id="stats")[0]
        assert c["document_id"] == "d9"
        assert c["source_filename"] == "stats.pdf"
        assert c["course_id"] == "stats"
        assert c["page"] == 7
        assert c["section_title"] == "Normal Distribution"

    def test_page_range_spans_the_blocks_in_the_chunk(self):
        blocks = [
            block(PARAGRAPH, "First.", page=3),
            block(PARAGRAPH, "Second.", page=4),
        ]
        c = chunk_all(blocks)[0]
        assert (c["page"], c["page_end"]) == (3, 4)

    def test_ids_are_deterministic_so_reingest_overwrites(self):
        blocks = [block(PARAGRAPH, PROSE)]
        first = chunk_all(blocks)
        second = chunk_all(blocks)
        assert [c["id"] for c in first] == [c["id"] for c in second]

    def test_different_documents_get_different_ids(self):
        blocks = [block(PARAGRAPH, PROSE)]
        a = chunk_all(blocks, document_id="a")[0]["id"]
        b = chunk_all(blocks, document_id="b")[0]["id"]
        assert a != b


class TestPineconeMetadata:
    @pytest.fixture
    def metadata(self):
        blocks = [
            block(HEADING, "Series", level=1),
            block(PARAGRAPH, PROSE, heading_path=["Series"], page=2,
                  equations=[r"\sum_{n=1}^\infty"]),
        ]
        return to_pinecone_metadata(chunk_all(blocks)[0])

    def test_contains_no_nulls(self, metadata):
        """Pinecone rejects null metadata values outright."""
        assert all(v is not None for v in metadata.values())

    def test_values_are_pinecone_safe_types(self, metadata):
        for key, value in metadata.items():
            assert isinstance(value, (str, int, float, bool, list)), key
            if isinstance(value, list):
                assert all(isinstance(v, str) for v in value), key

    def test_carries_everything_a_citation_needs(self, metadata):
        for field in ("document_id", "source_filename", "text", "section_title", "page"):
            assert field in metadata

    def test_empty_heading_path_is_omitted_not_null(self):
        metadata = to_pinecone_metadata(chunk_all([block(PARAGRAPH, PROSE)])[0])
        assert "heading_path" not in metadata


def test_content_before_the_first_heading_is_kept():
    """Cover pages and abstracts precede any heading."""
    blocks = [
        block(PARAGRAPH, "This handout accompanies the first lecture."),
        block(HEADING, "Limits", level=1),
        block(PARAGRAPH, PROSE, heading_path=["Limits"]),
    ]
    combined = " ".join(c["text"] for c in chunk_all(blocks))
    assert "accompanies the first lecture" in combined


def test_empty_document_yields_no_chunks():
    assert chunk_all([]) == []
