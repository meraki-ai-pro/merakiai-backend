"""Question-format and difficulty tags on uploads, and what they do.

The design decision under test: the tags are a PREFERENCE, not a filter.
Treating a non-match as "not visible" would empty a course's Review material
the first time one lecturer ticked "Multiple Choice" on one file, and the
lecturer would have no way to tell that had happened — retrieval would just go
quiet and the tutor would answer from general knowledge with a disclaimer.
"""

import pytest

from app.ai.ingestion.service import parse_question_formats
from app.ai.rag.visibility import prefer


def doc(did, formats=None, difficulty=None):
    return {"id": did, "question_formats": formats, "difficulty": difficulty}


def ids(rows):
    return [r["id"] for r in rows]


class TestParsing:
    def test_absent_means_any_format(self):
        assert parse_question_formats(None) is None
        assert parse_question_formats("  ") is None

    def test_parses_a_list(self):
        assert parse_question_formats("mcq,short_answer") == ["mcq", "short_answer"]

    def test_accepts_the_words_a_lecturer_would_type(self):
        assert parse_question_formats("Multiple Choice, Fill in the blank") == [
            "mcq", "fill_blank",
        ]

    def test_deduplicates_preserving_order(self):
        assert parse_question_formats("short_answer,mcq,short_answer") == [
            "short_answer", "mcq",
        ]

    def test_rejects_flashcard(self):
        """Removed from the student picker at the client's request, so material
        must not be taggable for a format nothing will generate."""
        with pytest.raises(ValueError, match="flashcard"):
            parse_question_formats("mcq,flashcard")

    @pytest.mark.parametrize("bad", ["essay", "true_false", "mcq,wizard"])
    def test_rejects_unknown_formats(self, bad):
        with pytest.raises(ValueError):
            parse_question_formats(bad)


class TestPreference:
    def test_no_preference_returns_everything(self):
        rows = [doc("a", ["mcq"]), doc("b")]
        assert prefer(rows) is rows

    def test_matching_documents_win(self):
        rows = [doc("mcqs", ["mcq"]), doc("shorts", ["short_answer"])]
        assert ids(prefer(rows, question_format="mcq")) == ["mcqs"]

    def test_untagged_documents_always_match(self):
        """A file uploaded before the field existed did not opt out of
        anything. Treating NULL as "unsuitable" would silently retire every
        pre-existing file the moment one new file was tagged."""
        rows = [doc("legacy"), doc("mcqs", ["mcq"])]
        assert ids(prefer(rows, question_format="short_answer")) == ["legacy"]

    def test_falls_back_to_everything_when_nothing_matches(self):
        """The whole reason this is a preference. An empty result here means a
        silent, unexplained gap in every answer."""
        rows = [doc("mcqs", ["mcq"]), doc("blanks", ["fill_blank"])]
        assert ids(prefer(rows, question_format="short_answer")) == ["mcqs", "blanks"]

    def test_difficulty_narrows_too(self):
        rows = [doc("easy", difficulty="basic"), doc("hard", difficulty="advanced")]
        assert ids(prefer(rows, difficulty="Advanced")) == ["hard"]

    def test_difficulty_and_format_must_both_hold(self):
        rows = [
            doc("right", ["mcq"], "basic"),
            doc("wrong_level", ["mcq"], "advanced"),
            doc("wrong_format", ["short_answer"], "basic"),
        ]
        assert ids(prefer(rows, question_format="mcq", difficulty="basic")) == ["right"]

    def test_case_and_whitespace_do_not_defeat_a_match(self):
        """difficulty arrives title-cased from the mode selector ("Basic") and
        lowercased from the documents table."""
        rows = [doc("a", difficulty="intermediate")]
        assert ids(prefer(rows, difficulty="  Intermediate ")) == ["a"]

    def test_empty_input_is_passed_through(self):
        assert prefer([], question_format="mcq") == []
        assert prefer(None, question_format="mcq") is None
