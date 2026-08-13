"""The retrieval evaluation harness.

Its job is to catch regressions, so the tests are mostly about whether the
scoring is honest — particularly for the cases people get wrong when they build
one of these by hand.
"""

from dataclasses import dataclass

import pytest

from app.ai.rag.evaluation import (
    GoldenCase,
    aggregate,
    load_golden_set,
    score_case,
)


@dataclass
class FakeChunk:
    text: str = ""
    parent_text: str = ""
    source_filename: str | None = None
    section_title: str | None = None
    heading_path: list | None = None
    dense_score: float = 0.9
    rerank_score: float | None = None

    def __post_init__(self):
        if self.heading_path is None:
            self.heading_path = []


class TestSourceMatching:
    def test_matches_on_filename(self):
        case = GoldenCase(question="q", course_id="c", expect_sources=["notes.pdf"])
        result = score_case(case, [FakeChunk(source_filename="lecture-notes.pdf")])
        assert result.hit

    def test_matches_on_section_title(self):
        case = GoldenCase(question="q", course_id="c", expect_sources=["reagents"])
        result = score_case(case, [FakeChunk(section_title="Flotation Reagents")])
        assert result.hit

    def test_matches_on_heading_path(self):
        case = GoldenCase(question="q", course_id="c", expect_sources=["surface"])
        result = score_case(case, [FakeChunk(heading_path=["Chapter 3", "Surface Chemistry"])])
        assert result.hit

    def test_case_insensitive(self):
        case = GoldenCase(question="q", course_id="c", expect_sources=["REAGENTS"])
        assert score_case(case, [FakeChunk(section_title="reagents")]).hit

    def test_records_what_was_missed(self):
        case = GoldenCase(question="q", course_id="c", expect_sources=["absent-thing"])
        result = score_case(case, [FakeChunk(section_title="something else")])
        assert not result.hit
        assert result.missed == ["absent-thing"]

    def test_text_expectations_search_the_body(self):
        case = GoldenCase(question="q", course_id="c", expect_text=["hydrophobic"])
        assert score_case(case, [FakeChunk(text="the surface becomes hydrophobic")]).hit

    def test_text_expectations_search_the_parent(self):
        """Small-to-big means the model reads the parent, so a match there
        counts."""
        case = GoldenCase(question="q", course_id="c", expect_text=["hydrophobic"])
        assert score_case(case, [FakeChunk(parent_text="... hydrophobic ...")]).hit


class TestRanking:
    def test_first_position_scores_one(self):
        case = GoldenCase(question="q", course_id="c", expect_sources=["target"])
        result = score_case(case, [FakeChunk(section_title="target"), FakeChunk()])
        assert result.reciprocal_rank == 1.0

    def test_second_position_scores_a_half(self):
        case = GoldenCase(question="q", course_id="c", expect_sources=["target"])
        result = score_case(case, [FakeChunk(), FakeChunk(section_title="target")])
        assert result.reciprocal_rank == 0.5

    def test_a_miss_scores_zero(self):
        case = GoldenCase(question="q", course_id="c", expect_sources=["target"])
        assert score_case(case, [FakeChunk()] * 5).reciprocal_rank == 0.0

    def test_precision_reflects_how_much_noise_came_back(self):
        case = GoldenCase(question="q", course_id="c", expect_sources=["target"])
        result = score_case(case, [FakeChunk(section_title="target")] + [FakeChunk()] * 3)
        assert result.precision == 0.25


class TestNegativeCases:
    def test_admitting_ignorance_is_a_pass(self):
        """The question the course does not cover. Correct behaviour is to
        retrieve nothing confident."""
        case = GoldenCase(question="capital of France?", course_id="c", expect_no_answer=True)
        assert score_case(case, []).hit

    def test_weak_retrieval_is_also_a_pass(self):
        case = GoldenCase(question="q", course_id="c", expect_no_answer=True)
        assert score_case(case, [FakeChunk(dense_score=0.05)]).hit

    def test_confidently_answering_an_uncovered_question_fails(self):
        """The failure mode that matters most: fluent, confident, wrong."""
        case = GoldenCase(question="q", course_id="c", expect_no_answer=True)
        assert not score_case(case, [FakeChunk(dense_score=0.95)]).hit

    def test_scored_inverted_not_by_the_same_rule(self):
        """Mixing them into the ordinary rule would mark correct behaviour as
        a failure."""
        negative = GoldenCase(question="q", course_id="c", expect_no_answer=True)
        positive = GoldenCase(question="q", course_id="c")
        empty: list = []
        assert score_case(negative, empty).hit
        assert not score_case(positive, empty).hit


class TestAggregation:
    def _results(self, hits):
        case = GoldenCase(question="q", course_id="c", expect_sources=["t"])
        return [
            score_case(case, [FakeChunk(section_title="t" if h else "x")])
            for h in hits
        ]

    def test_recall_is_the_hit_fraction(self):
        assert aggregate(self._results([True, True, False, False]))["recall_at_k"] == 0.5

    def test_empty_run_reports_zero_cases(self):
        assert aggregate([])["cases"] == 0

    def test_failures_are_listed_for_action(self):
        summary = aggregate(self._results([True, False]))
        assert len(summary["failures"]) == 1

    def test_verdict_breakdown_is_present(self):
        summary = aggregate(self._results([True]))
        assert set(summary["verdicts"]) == {"correct", "ambiguous", "incorrect"}


class TestGoldenSetLoading:
    def test_reads_jsonl_and_skips_comments(self, tmp_path):
        f = tmp_path / "g.jsonl"
        f.write_text(
            '// a comment\n'
            '{"question": "a", "course_id": "c"}\n'
            '\n'
            '{"question": "b", "course_id": "c", "expect_sources": ["x"]}\n',
            encoding="utf-8",
        )
        cases = load_golden_set(f)
        assert len(cases) == 2
        assert cases[1].expect_sources == ["x"]

    def test_a_bad_line_names_its_line_number(self, tmp_path):
        """One malformed line must not invalidate the whole file silently."""
        f = tmp_path / "g.jsonl"
        f.write_text('{"question": "a", "course_id": "c"}\n{not json}\n', encoding="utf-8")
        with pytest.raises(ValueError, match=":2:"):
            load_golden_set(f)

    def test_the_shipped_example_set_parses(self):
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "evals" / "froth-flotation.jsonl"
        cases = load_golden_set(path)
        assert len(cases) >= 10
        # The category people leave out, and the one that catches confident
        # wrong answers.
        assert any(c.expect_no_answer for c in cases)
