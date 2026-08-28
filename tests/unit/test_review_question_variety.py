"""A ten-question review must ask ten questions, not one question ten times.

Found by driving Review in a browser rather than by any API assertion: the
endpoint was healthy, the difficulty adapted Basic -> Intermediate, the grader
graded correctly — and question 2 was question 1 with the options shuffled and
the same `Question ID: REV-MCQ-0001` printed above it.

The cause was that `generate_review_item` had no memory. Every call in a
session used the same course, the same difficulty band and the same retrieval
seed, so it retrieved the same chunks and produced the same question. Nothing
in the pipeline compared question N against question N-1, so nothing noticed.
"""

import json

import pytest

from app.ai.rag.modes_sessions import service


ITEM = {
    "type": "mcq",
    "question_id": "REV-MCQ-XXXX",
    "difficulty": "Basic",
    "category": "Related Rates",
    "question": "A balloon is inflated at 2 cm/s. How fast is the volume growing?",
    "options": {"A": "100pi", "B": "200pi", "C": "400pi", "D": "500pi"},
}


class TestTheGeneratorIsToldWhatItAlreadyAsked:
    def test_prior_questions_reach_the_prompt(self):
        block = service._already_asked_block(
            [service.asked_summary(ITEM)]
        )
        assert ITEM["question"] in block, (
            "the previous question is not in the prompt, so the model has no way "
            "to avoid repeating it"
        )
        assert "Related Rates" in block
        assert "do NOT reword" in block

    def test_an_empty_history_adds_nothing(self):
        """Question 1 must not pay for a block that says nothing."""
        assert service._already_asked_block([]) == ""

    def test_items_with_no_text_are_skipped_rather_than_listed_blank(self):
        assert service._already_asked_block([{"category": "x"}]) == ""

    @pytest.mark.parametrize(
        "item, expected",
        [
            ({"question": "  q  "}, "q"),
            ({"sentence_with_blank": "The ____ rule."}, "The ____ rule."),
            ({"statement": "Limits are exact."}, "Limits are exact."),
            ({}, ""),
        ],
    )
    def test_every_format_yields_its_identifying_line(self, item, expected):
        """Fill-in-the-blank and true/false do not have a `question` key. If the
        summariser only understood MCQs, those formats would carry an empty
        history and repeat silently."""
        assert service._question_text(item) == expected

    def test_the_summary_stays_small(self):
        """History is stored on the session and grows every turn."""
        summary = service.asked_summary({**ITEM, "options": {"A": "x" * 5000}})
        assert set(summary) == {"question", "category"}


class TestTheRealGeneratorAppliesAllOfThis:
    """Drives generate_review_item itself with the model and the retriever
    stubbed out.

    The earlier version of these tests recomputed `min(6 + 2 * n, 20)` in the
    test and asserted it equalled itself, which passes whatever the function
    does. These capture what the function actually sends.
    """

    @staticmethod
    def _install(monkeypatch, seen):
        async def fake_retrieve(**kwargs):
            seen["retrieve"] = kwargs
            return ["Reference chunk about related rates."]

        async def fake_generate(prompt, mode, system_parts, **_):
            seen["prompt"] = prompt
            return json.dumps(ITEM)

        monkeypatch.setattr(service, "retrieve_context", fake_retrieve)
        monkeypatch.setattr(service, "generate_response", fake_generate)

    async def _run(self, monkeypatch, asked):
        seen: dict = {}
        self._install(monkeypatch, seen)
        item, _ = await service.generate_review_item(
            session_type="mcq",
            difficulty="Basic",
            course_id="c1",
            course_name="Calculus I",
            difficulty_descriptors={},
            asked=asked,
        )
        return item, seen

    @pytest.mark.asyncio
    async def test_the_first_question_is_numbered_one_not_the_models_guess(
        self, monkeypatch
    ):
        item, _ = await self._run(monkeypatch, asked=[])
        assert item["question_id"] == "REV-MCQ-0001"

    @pytest.mark.asyncio
    async def test_the_third_question_is_numbered_three(self, monkeypatch):
        """The model returns the same REV-MCQ-XXXX placeholder every call; the
        student saw `REV-MCQ-0001` above two different questions."""
        prior = [service.asked_summary(ITEM), service.asked_summary(ITEM)]
        item, _ = await self._run(monkeypatch, asked=prior)
        assert item["question_id"] == "REV-MCQ-0003"

    @pytest.mark.asyncio
    async def test_fill_in_the_blank_is_numbered_under_its_own_key(
        self, monkeypatch
    ):
        seen: dict = {}

        async def fake_retrieve(**kwargs):
            return ["ref"]

        async def fake_generate(prompt, mode, system_parts, **_):
            return json.dumps(
                {"type": "fill_blank", "sentence_with_blank": "The ____ rule."}
            )

        monkeypatch.setattr(service, "retrieve_context", fake_retrieve)
        monkeypatch.setattr(service, "generate_response", fake_generate)

        item, _ = await service.generate_review_item(
            session_type="fill_blank",
            difficulty="Basic",
            course_id="c1",
            course_name="Calculus I",
            difficulty_descriptors={},
            asked=[service.asked_summary(ITEM)],
        )
        assert item["item_id"] == "REV-FB-0002"
        assert "question_id" not in item

    @pytest.mark.asyncio
    async def test_the_previous_question_reaches_the_model(self, monkeypatch):
        _, seen = await self._run(
            monkeypatch, asked=[service.asked_summary(ITEM)]
        )
        assert ITEM["question"] in seen["prompt"]

    @pytest.mark.asyncio
    async def test_question_one_is_not_told_about_a_history_it_has_not_got(
        self, monkeypatch
    ):
        _, seen = await self._run(monkeypatch, asked=[])
        assert "ALREADY ASKED" not in seen["prompt"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asked_count, expected_top_k", [(0, 6), (1, 8), (7, 20), (50, 20)])
    async def test_retrieval_widens_as_the_session_runs_then_caps(
        self, monkeypatch, asked_count, expected_top_k
    ):
        """An embedding query cannot express "not this", so variety is bought
        with breadth. The cap keeps a long session from dragging the whole
        course into every prompt."""
        prior = [service.asked_summary(ITEM)] * asked_count
        _, seen = await self._run(monkeypatch, asked=prior)
        assert seen["retrieve"]["top_k"] == expected_top_k

    @pytest.mark.asyncio
    async def test_the_history_is_optional(self, monkeypatch):
        """Callers that predate this argument must keep working."""
        seen: dict = {}
        self._install(monkeypatch, seen)
        item, _ = await service.generate_review_item(
            session_type="mcq",
            difficulty="Basic",
            course_id="c1",
            course_name="Calculus I",
            difficulty_descriptors={},
        )
        assert item["question_id"] == "REV-MCQ-0001"
