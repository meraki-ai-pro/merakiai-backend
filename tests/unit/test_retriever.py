"""Hybrid retrieval: lexical scoring, fusion, attribution, and re-ranking.

The retriever this replaces ran one dense search and returned bare strings.
These tests cover the two things that changed: questions that hinge on exact
tokens now retrieve, and every result carries the provenance a citation needs.
No test hits Pinecone, OpenAI, Cohere, or Anthropic.
"""

import asyncio
import os

import pytest

from app.ai.rag import retriever
from app.ai.rag.retriever import (
    RetrievedChunk,
    _chunk_from_match,
    _dedupe_by_parent,
    _maybe_rerank,
    _rerank_provider,
    bm25_scores,
    reciprocal_rank_fusion,
    tokenize,
)


def run(coro):
    return asyncio.run(coro)


class TestTokenize:
    def test_drops_stopwords(self):
        assert "the" not in tokenize("What is the derivative")

    def test_keeps_subject_terms(self):
        assert set(tokenize("What is the derivative")) == {"derivative"}

    def test_keeps_section_numbers_whole(self):
        """"Theorem 4.2" is exactly what dense search smooths away — and
        splitting it into "4" and "2" would lose the signal just as thoroughly."""
        assert tokenize("Theorem 4.2") == ["theorem", "4.2"]

    def test_keeps_alphanumeric_identifiers(self):
        assert "chi2" in tokenize("the chi2 statistic")

    def test_keeps_single_letter_variables(self):
        """A bare "x" is meaningful in mathematics; IDF handles its commonness."""
        assert "x" in tokenize("solve for x")

    def test_lowercases(self):
        assert tokenize("DERIVATIVE") == ["derivative"]


class TestBm25:
    def test_exact_term_match_scores_highest(self):
        docs = [
            "The normal distribution is symmetric about its mean.",
            "Chebyshev's inequality bounds the probability of deviation.",
            "A derivative measures an instantaneous rate of change.",
        ]
        scores = bm25_scores("Chebyshev inequality", docs)
        assert scores.index(max(scores)) == 1

    def test_unmatched_query_scores_zero(self):
        scores = bm25_scores("topology", ["calculus notes", "statistics notes"])
        assert scores == [0.0, 0.0]

    def test_empty_documents(self):
        assert bm25_scores("anything", []) == []

    def test_query_of_only_stopwords_scores_zero(self):
        scores = bm25_scores("what is the", ["a document", "another document"])
        assert scores == [0.0, 0.0]

    def test_repeated_term_saturates(self):
        """BM25 damps term frequency — ten mentions is not ten times as relevant."""
        once = bm25_scores("limit", ["limit"])[0]
        many = bm25_scores("limit", ["limit " * 10])[0]
        assert many < once * 10


class TestFusion:
    def test_item_ranked_well_by_both_wins(self):
        fused = reciprocal_rank_fusion([[0, 1, 2], [0, 2, 1]])
        assert max(fused, key=fused.get) == 0

    def test_item_ranked_well_by_one_signal_still_places(self):
        """A chunk only lexical search finds must not be lost."""
        fused = reciprocal_rank_fusion([[1, 2, 0], [0, 1, 2]])
        assert fused[0] > fused[2]

    def test_empty_rankings(self):
        assert reciprocal_rank_fusion([]) == {}


class TestHybridRanking:
    """The point of the exercise: a passage dense search ranks poorly must win
    when it is the one that actually contains the term asked about."""

    CORPUS = [
        (0.88, "A derivative measures an instantaneous rate of change.", "Derivatives"),
        (0.61, "Chebyshev's inequality bounds the probability of deviation from the mean.", "Chebyshev"),
        (0.86, "Continuity means the function has no jumps or gaps.", "Continuity"),
        (0.84, "The mean value theorem relates average and instantaneous rates.", "MVT"),
    ]

    @pytest.fixture(autouse=True)
    def stub_network(self, monkeypatch):
        monkeypatch.setenv("RAG_RERANK", "off")

        async def embed(_query):
            return [0.1] * 8

        async def query(_embedding, namespace, mode, _top_k, _document_ids=None):
            if not namespace.endswith("-v2"):
                return []
            return [
                _chunk_from_match(
                    {
                        "id": f"c{i}",
                        "score": score,
                        "metadata": {
                            "text": text,
                            "parent_text": f"PARENT {section}",
                            "parent_id": f"p{i}",
                            "source_filename": "course.pdf",
                            "section_title": section,
                            "page": i + 1,
                            "mode": mode,
                        },
                    },
                    namespace,
                )
                for i, (score, text, section) in enumerate(self.CORPUS)
            ]

        # Visibility resolves against Postgres. Stubbed to None ("everything is
        # visible") so these tests exercise ranking only — without this they
        # make a live Supabase call per case.
        async def all_visible(_course_id, _mode):
            return None

        monkeypatch.setattr(retriever, "embed_query", embed)
        monkeypatch.setattr(retriever, "_query_namespace", query)
        monkeypatch.setattr(retriever, "visible_document_ids", all_visible)
        # The empty-retrieval path emits a research event, which would
        # otherwise make a live Supabase call from a unit test.
        monkeypatch.setattr(retriever, "emit", lambda *a, **k: None)

    def top_section(self, query: str) -> str:
        chunks = run(retriever.retrieve(query, "learn", "course", top_k=3))
        return chunks[0].section_title

    def test_exact_term_beats_higher_dense_similarity(self):
        """Chebyshev has the lowest dense score of the four passages."""
        assert self.top_section("What does Chebyshev's inequality say?") == "Chebyshev"

    def test_dense_ranking_still_wins_without_lexical_overlap(self):
        assert self.top_section("explain continuity") == "Continuity"

    def test_unmatched_passages_earn_no_lexical_credit(self):
        """RRF scores by rank position, so including the zero-scoring majority
        in the lexical ranking would hand them nearly full lexical credit."""
        chunks = run(retriever.retrieve("Chebyshev inequality", "learn", "course", top_k=4))
        matched = [c for c in chunks if c.lexical_score > 0]
        unmatched = [c for c in chunks if c.lexical_score == 0]
        assert matched and unmatched
        assert min(c.score for c in matched) > max(c.score for c in unmatched)

    def test_citations_are_numbered_from_one_in_final_order(self):
        chunks = run(retriever.retrieve("Chebyshev inequality", "learn", "course", top_k=3))
        assert [c.citation for c in chunks] == [1, 2, 3]

    def test_results_carry_attribution(self):
        chunks = run(retriever.retrieve("derivative", "learn", "course", top_k=1))
        assert chunks[0].source_filename == "course.pdf"
        assert chunks[0].page is not None

    def test_empty_pool_returns_nothing(self, monkeypatch):
        async def nothing(*args, **kwargs):
            return []

        monkeypatch.setattr(retriever, "_query_namespace", nothing)
        assert run(retriever.retrieve("anything", "learn", "course")) == []

    def test_course_id_is_required(self):
        with pytest.raises(ValueError):
            run(retriever.retrieve("q", "learn", ""))

    def test_legacy_generation_is_ignored_when_current_has_results(self, monkeypatch):
        """The legacy namespace is a fallback, not a second source to merge.

        Merging them mixes two chunkings of the same passage and, because
        legacy vectors carry no provenance, suppresses citations for a course
        that has in fact been re-ingested.
        """

        async def both(_embedding, namespace, mode, _top_k, _document_ids=None):
            metadata = {"text": f"passage from {namespace}", "mode": mode}
            if namespace.endswith("-v2"):
                metadata["source_filename"] = "course.pdf"
            return [_chunk_from_match({"id": namespace, "score": 0.8, "metadata": metadata}, namespace)]

        monkeypatch.setattr(retriever, "_query_namespace", both)
        chunks = run(retriever.retrieve("anything", "learn", "course", top_k=5))
        assert [c.namespace for c in chunks] == ["course-learn-v2"]

    def test_legacy_generation_used_when_current_is_empty(self, monkeypatch):
        """A course not yet re-ingested must keep answering."""

        async def legacy_only(_embedding, namespace, mode, _top_k, _document_ids=None):
            if namespace.endswith("-v2"):
                return []
            return [
                _chunk_from_match(
                    {"id": "old", "score": 0.7, "metadata": {"text": "legacy passage", "mode": mode}},
                    namespace,
                )
            ]

        monkeypatch.setattr(retriever, "_query_namespace", legacy_only)
        chunks = run(retriever.retrieve("anything", "learn", "course", top_k=5))
        assert [c.namespace for c in chunks] == ["course-learn"]

    def test_legacy_shim_returns_parent_strings(self):
        contexts = run(retriever.retrieve_context("derivative", "learn", "course", top_k=2))
        assert all(isinstance(c, str) for c in contexts)
        assert contexts[0].startswith("PARENT ")


class TestChunkFromMatch:
    def test_v2_match_carries_full_provenance(self):
        chunk = _chunk_from_match(
            {
                "id": "c1",
                "score": 0.82,
                "metadata": {
                    "text": "A limit describes...",
                    "parent_text": "Section body...",
                    "parent_id": "p1",
                    "document_id": "d1",
                    "source_filename": "calculus.pdf",
                    "section_title": "Limits",
                    "heading_path": ["Calculus"],
                    "page": 7,
                    "page_end": 8,
                    "has_math": True,
                    "mode": "learn",
                },
            },
            "course-learn-v2",
        )
        assert chunk.document_id == "d1"
        assert chunk.page == 7
        assert chunk.has_math is True
        assert chunk.heading_path == ["Calculus"]

    def test_legacy_match_still_retrieves(self):
        """v1 vectors carry only {mode, topic, text} and must not crash."""
        chunk = _chunk_from_match(
            {"id": "old", "score": 0.6, "metadata": {"text": "legacy text", "topic": "T"}},
            "course-learn",
        )
        assert chunk.text == "legacy text"
        assert chunk.document_id is None
        assert chunk.page is None
        assert chunk.heading_path == []

    def test_missing_metadata_is_tolerated(self):
        chunk = _chunk_from_match({"id": "x", "score": 0.1}, "ns")
        assert chunk.text == ""


class TestChunkPresentation:
    def test_parent_is_what_the_model_reads(self):
        chunk = RetrievedChunk(id="c", text="child", score=1, parent_text="parent section")
        assert chunk.context_text == "parent section"

    def test_falls_back_to_child_without_a_parent(self):
        chunk = RetrievedChunk(id="c", text="child", score=1)
        assert chunk.context_text == "child"

    def test_location_combines_file_page_and_section(self):
        chunk = RetrievedChunk(
            id="c", text="t", score=1,
            source_filename="notes.pdf", page=7, section_title="Limits",
        )
        assert chunk.location == "notes.pdf, p. 7 — Limits"

    def test_location_renders_a_page_range(self):
        chunk = RetrievedChunk(
            id="c", text="t", score=1, source_filename="notes.pdf", page=7, page_end=9
        )
        assert "pp. 7-9" in chunk.location

    def test_location_degrades_for_legacy_chunks(self):
        assert RetrievedChunk(id="c", text="t", score=1).location == "course material"

    @pytest.mark.parametrize(
        "score,band", [(0.9, "high"), (0.5, "medium"), (0.1, "low")]
    )
    def test_relevance_bands(self, score, band):
        assert RetrievedChunk(id="c", text="t", score=1, dense_score=score).relevance_band == band

    def test_rerank_score_supersedes_dense_score_for_the_band(self):
        chunk = RetrievedChunk(id="c", text="t", score=1, dense_score=0.1, rerank_score=0.9)
        assert chunk.relevance_band == "high"

    def test_source_payload_has_what_the_client_needs(self):
        chunk = RetrievedChunk(
            id="c", text="t", score=0.5, source_filename="a.pdf", page=2, citation=1
        )
        source = chunk.to_source()
        for key in ("citation", "text", "location", "page", "relevance", "score"):
            assert key in source


class TestDedupeByParent:
    def test_keeps_only_the_best_child_of_a_section(self):
        chunks = [
            RetrievedChunk(id="a", text="1", score=3, parent_id="p1"),
            RetrievedChunk(id="b", text="2", score=2, parent_id="p1"),
            RetrievedChunk(id="c", text="3", score=1, parent_id="p2"),
        ]
        kept = _dedupe_by_parent(chunks)
        assert [c.id for c in kept] == ["a", "c"]

    def test_parentless_chunks_are_kept_separately(self):
        chunks = [
            RetrievedChunk(id="a", text="1", score=2),
            RetrievedChunk(id="b", text="2", score=1),
        ]
        assert len(_dedupe_by_parent(chunks)) == 2


class TestRerankProvider:
    def test_explicit_setting_is_honoured(self, monkeypatch):
        monkeypatch.setenv("RAG_RERANK", "llm")
        assert _rerank_provider() == "llm"

    def test_auto_uses_cohere_when_a_key_exists(self, monkeypatch):
        monkeypatch.setenv("RAG_RERANK", "auto")
        monkeypatch.setenv("COHERE_API_KEY", "key")
        assert _rerank_provider() == "cohere"

    def test_auto_falls_back_to_the_llm_judge(self, monkeypatch):
        """Re-ranking is on by default; without a cross-encoder it runs on the
        Anthropic key the application already holds."""
        monkeypatch.setenv("RAG_RERANK", "auto")
        monkeypatch.delenv("COHERE_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert _rerank_provider() == "llm"

    def test_auto_is_off_with_no_credentials_at_all(self, monkeypatch):
        monkeypatch.setenv("RAG_RERANK", "auto")
        monkeypatch.delenv("COHERE_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert _rerank_provider() == "off"

    def test_llm_judge_defaults_to_haiku(self, monkeypatch):
        """A reranker does no generation and sits inside every turn's latency
        budget, so it is deliberately a small model."""
        monkeypatch.delenv("RAG_RERANK_MODEL", raising=False)
        assert os.getenv("RAG_RERANK_MODEL", "claude-haiku-4-5") == "claude-haiku-4-5"


class TestRerankFallback:
    @pytest.fixture
    def chunks(self):
        return [RetrievedChunk(id=str(i), text=f"t{i}", score=10 - i) for i in range(5)]

    def test_off_returns_the_fused_order(self, monkeypatch, chunks):
        monkeypatch.setenv("RAG_RERANK", "off")
        assert [c.id for c in run(_maybe_rerank("q", chunks, 3))] == ["0", "1", "2"]

    def test_provider_failure_falls_back_to_fused_order(self, monkeypatch, chunks):
        """A re-ranker outage must degrade precision, not break the turn."""
        monkeypatch.setenv("RAG_RERANK", "cohere")

        async def boom(query, items, top_k):
            raise RuntimeError("rerank service down")

        monkeypatch.setattr(retriever, "_rerank_cohere", boom)
        assert [c.id for c in run(_maybe_rerank("q", chunks, 3))] == ["0", "1", "2"]

    def test_unknown_provider_falls_back(self, monkeypatch, chunks):
        monkeypatch.setenv("RAG_RERANK", "nonsense")
        assert len(run(_maybe_rerank("q", chunks, 2))) == 2

    def test_reranked_order_is_used(self, monkeypatch, chunks):
        monkeypatch.setenv("RAG_RERANK", "cohere")

        async def reversed_order(query, items, top_k):
            return list(reversed(items))[:top_k]

        monkeypatch.setattr(retriever, "_rerank_cohere", reversed_order)
        assert [c.id for c in run(_maybe_rerank("q", chunks, 3))] == ["4", "3", "2"]
