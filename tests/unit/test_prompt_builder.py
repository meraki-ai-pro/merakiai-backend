"""Prompt assembly: maths instruction, response formats, and citations."""

import pytest

from app.ai.rag.prompt_builder import _build_reference_block, build_system_and_user


def source(n: int, **overrides):
    base = {
        "citation": n,
        "location": f"notes.pdf, p. {n} — Section {n}",
        "source_filename": "notes.pdf",
        "section_title": f"Section {n}",
        "page": n,
    }
    base.update(overrides)
    return base


class TestMathsInstruction:
    def test_learn_mode_requires_latex(self):
        """The prompt previously said "Avoid equations unless absolutely
        necessary", which is fatal for a mathematics course."""
        system, _ = build_system_and_user("q", ["ctx"], "learn")
        assert "Avoid equations" not in system
        assert "LaTeX" in system

    def test_learn_mode_requires_shown_working(self):
        system, _ = build_system_and_user("q", ["ctx"], "learn")
        assert "every step" in system

    def test_review_mode_still_returns_json(self):
        system, _ = build_system_and_user("q", ["ctx"], "review")
        assert "valid JSON" in system

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            build_system_and_user("q", ["ctx"], "nonsense")


class TestResponseFormatDirectives:
    def test_board_directive_applied(self):
        _, user = build_system_and_user("q", ["ctx"], "learn", board=True)
        assert "::: slide" in user

    def test_video_brevity_directive_applied(self):
        _, user = build_system_and_user("q", ["ctx"], "learn", concise=True)
        assert "SPOKEN VIDEO ANSWER" in user

    def test_brevity_wins_over_board(self):
        """A slide deck is not a 90-second spoken monologue."""
        _, user = build_system_and_user("q", ["ctx"], "learn", concise=True, board=True)
        assert "::: slide" not in user
        assert "SPOKEN VIDEO ANSWER" in user

    def test_neither_by_default(self):
        _, user = build_system_and_user("q", ["ctx"], "learn")
        assert "::: slide" not in user
        assert "SPOKEN VIDEO ANSWER" not in user

    def test_directives_stay_out_of_the_cached_system_prefix(self):
        """Putting a per-turn directive in the system text would invalidate the
        prompt cache on every turn."""
        plain, _ = build_system_and_user("q", ["ctx"], "learn")
        boarded, _ = build_system_and_user("q", ["ctx"], "learn", board=True)
        assert plain == boarded


class TestCitations:
    def test_sources_produce_numbered_passages(self):
        block = _build_reference_block(["first", "second"], [source(1), source(2)])
        assert "[1] (notes.pdf, p. 1 — Section 1)" in block
        assert "[2] (notes.pdf, p. 2 — Section 2)" in block

    def test_citation_instruction_included_with_sources(self):
        block = _build_reference_block(["a"], [source(1)])
        assert "CITING YOUR SOURCES" in block

    def test_no_citation_instruction_without_sources(self):
        """Legacy-namespace chunks cannot be identified, so asking for
        citations there would invite invented ones."""
        block = _build_reference_block(["a", "b"], None)
        assert "CITING YOUR SOURCES" not in block
        assert "[1]" not in block

    def test_context_still_present_without_sources(self):
        block = _build_reference_block(["alpha", "beta"], None)
        assert "alpha" in block and "beta" in block

    def test_model_told_not_to_invent_numbers(self):
        """Was a general "never invent a number"; live testing showed that is
        not enough — the ceiling has to be named."""
        block = _build_reference_block(["a"], [source(1)])
        assert "ONLY VALID CITATION NUMBERS ARE [1] TO [1]" in block
        assert "There is no [2]" in block

    def test_model_told_to_admit_gaps(self):
        block = _build_reference_block(["a"], [source(1)])
        assert "do not cover what was asked" in block

    def test_empty_context_is_stated_explicitly(self):
        """Silence here would read as "no reference material was relevant"."""
        assert "(none retrieved)" in _build_reference_block([], None)

    def test_sources_reach_the_user_text(self):
        _, user = build_system_and_user(
            "q", ["passage one"], "learn", sources=[source(1)]
        )
        assert "[1]" in user
        assert "CITING YOUR SOURCES" in user

    def test_location_falls_back_when_missing(self):
        block = _build_reference_block(["a"], [{"citation": 1}])
        assert "(course material)" in block


class TestCitationCeiling:
    """Live testing produced a [7] against six sources. The model invents a
    plausible next number unless the ceiling is named concretely."""

    def _user_text(self, n):
        from app.ai.rag.prompt_builder import build_system_and_user

        sources = [{"location": f"notes.pdf, p. {i}"} for i in range(1, n + 1)]
        _, user = build_system_and_user(
            user_message="q",
            context=[f"passage {i}" for i in range(1, n + 1)],
            mode="learn",
            sources=sources,
        )
        return user

    def test_the_range_is_stated_explicitly(self):
        assert "[1] TO [6]" in self._user_text(6)

    def test_the_first_invalid_number_is_named(self):
        """Naming [7] is more concrete than 'do not invent one'."""
        assert "There is no [7]" in self._user_text(6)

    def test_the_ceiling_tracks_the_source_count(self):
        assert "[1] TO [3]" in self._user_text(3)
        assert "There is no [4]" in self._user_text(3)

    def test_no_citation_rules_without_sources(self):
        from app.ai.rag.prompt_builder import build_system_and_user

        _, user = build_system_and_user(
            user_message="q", context=["passage"], mode="learn", sources=None
        )
        assert "VALID CITATION NUMBERS" not in user
