"""Corrective RAG and progressive scaffolding.

The CRAG tests are about a specific failure: retrieval returns five loosely
related passages, the model is told to ground its answer in them, and it
produces something fluent and wrong. A student cannot detect that, which makes
it the worst failure mode a teaching system has.
"""

from dataclasses import dataclass

import pytest

from app.ai.rag import crag
from app.ai.rag.crag import AMBIGUOUS, CORRECT, INCORRECT, STRONG, WEAK, assess
from app.ai.rag.prompt_builder import build_system_and_user, scaffolding_for
from app.core.academic_levels import LEVEL_CODES, normalise, tier_for


@dataclass
class FakeChunk:
    dense_score: float = 0.0
    rerank_score: float | None = None


class TestRetrievalAssessment:
    def test_no_chunks_is_a_failure(self):
        assert assess([]).verdict == INCORRECT

    def test_a_strong_match_is_correct(self):
        assert assess([FakeChunk(dense_score=STRONG + 0.1)]).verdict == CORRECT

    def test_a_middling_match_is_worth_retrying(self):
        assert assess([FakeChunk(dense_score=(STRONG + WEAK) / 2)]).verdict == AMBIGUOUS

    def test_only_poor_matches_is_a_failure(self):
        assert assess([FakeChunk(dense_score=WEAK - 0.1)] * 5).verdict == INCORRECT

    def test_judged_on_the_best_chunk_not_the_mean(self):
        """One strongly relevant passage is enough to answer from. A mean is
        dragged down by the candidate pool's long tail and would condemn
        perfectly good retrievals."""
        chunks = [FakeChunk(dense_score=0.95)] + [FakeChunk(dense_score=0.05)] * 9
        assert assess(chunks).verdict == CORRECT

    def test_rerank_score_wins_over_dense(self):
        """When a re-ranker has run, its opinion is the better signal."""
        chunk = FakeChunk(dense_score=0.05, rerank_score=0.95)
        assert assess([chunk]).verdict == CORRECT

    def test_usable_counts_only_chunks_above_the_floor(self):
        chunks = [FakeChunk(dense_score=0.9), FakeChunk(dense_score=0.1)]
        assert assess(chunks).usable == 1

    def test_verdict_helpers_are_mutually_exclusive(self):
        for score in (0.05, 0.5, 0.95):
            a = assess([FakeChunk(dense_score=score)])
            assert not (a.should_retry and a.should_admit_failure)

    def test_thresholds_are_ordered(self):
        assert 0 < WEAK < STRONG < 1


class TestQueryRewriting:
    @pytest.mark.asyncio
    async def test_failure_returns_the_original(self, monkeypatch):
        """A rewrite is an optimisation; losing it must never lose the turn."""
        async def boom(**_k):
            raise RuntimeError("model unavailable")

        monkeypatch.setattr("app.ai.rag.claude.generate_response", boom)
        assert await crag.rewrite_query("what is a limit?") == "what is a limit?"

    @pytest.mark.asyncio
    async def test_empty_output_falls_back(self, monkeypatch):
        async def blank(**_k):
            return "   "

        monkeypatch.setattr("app.ai.rag.claude.generate_response", blank)
        assert await crag.rewrite_query("what is a limit?") == "what is a limit?"

    @pytest.mark.asyncio
    async def test_runaway_output_falls_back(self, monkeypatch):
        """A rewrite that ballooned is a bad rewrite, not a better query."""
        async def essay(**_k):
            return "x" * 5000

        monkeypatch.setattr("app.ai.rag.claude.generate_response", essay)
        assert await crag.rewrite_query("q") == "q"

    @pytest.mark.asyncio
    async def test_takes_the_first_line_and_strips_quotes(self, monkeypatch):
        async def chatty(**_k):
            return '"the limit of a rational function"\nHope that helps!'

        monkeypatch.setattr("app.ai.rag.claude.generate_response", chatty)
        assert await crag.rewrite_query("q") == "the limit of a rational function"


class TestHonestFailureDirective:
    def test_absent_by_default(self):
        _, user = build_system_and_user(user_message="q", context=["c"], mode="learn")
        assert "RETRIEVAL WAS WEAK" not in user

    def test_present_when_retrieval_failed(self):
        _, user = build_system_and_user(
            user_message="q", context=["c"], mode="learn", insufficient_context=True
        )
        assert "RETRIEVAL WAS WEAK" in user

    def test_it_forbids_assembling_an_answer_from_loose_passages(self):
        _, user = build_system_and_user(
            user_message="q", context=["c"], mode="learn", insufficient_context=True
        )
        assert "only loosely related" in user

    def test_it_rides_the_dynamic_text_not_the_cached_prefix(self):
        """It varies per turn; in the system text it would poison the cache."""
        system, user = build_system_and_user(
            user_message="q", context=["c"], mode="learn", insufficient_context=True
        )
        assert "RETRIEVAL WAS WEAK" in user
        assert "RETRIEVAL WAS WEAK" not in system


class TestDeterministicHonestFailure:
    def test_plain_answer_gets_a_course_material_notice(self):
        from app.ai.rag.service import _ensure_failure_disclaimer

        answer, suffix = _ensure_failure_disclaimer(
            "Here is a general explanation.", board=False, concise=False
        )

        assert "reference material from your course notes" in answer
        assert suffix and answer.endswith(suffix)

    def test_board_answer_gets_a_final_notice_slide(self):
        from app.ai.rag.service import _ensure_failure_disclaimer

        answer, suffix = _ensure_failure_disclaimer(
            "::: slide Explanation\nGeneral knowledge.\n:::",
            board=True,
            concise=False,
        )

        assert "::: slide Course material note" in suffix
        assert answer.endswith(":::")
        assert "reference material" in suffix

    def test_model_written_notice_is_not_duplicated(self):
        from app.ai.rag.service import _ensure_failure_disclaimer

        original = "I do not have reference material for that topic."
        answer, suffix = _ensure_failure_disclaimer(
            original, board=True, concise=False
        )

        assert answer == original
        assert suffix == ""


class TestProgressiveScaffolding:
    """Ghanaian levels — students say "Level 200", not "intermediate"."""

    @pytest.mark.parametrize("level", LEVEL_CODES)
    def test_every_level_has_an_instruction(self, level):
        assert scaffolding_for(level)

    def test_unknown_levels_add_nothing(self):
        """Guessing wrong patronises a Masters student or strands a Level 100
        one. Silence preserves the behaviour the pilot is tuned for."""
        assert scaffolding_for(None) == ""
        assert scaffolding_for("postgraduate-ish") == ""

    @pytest.mark.parametrize(
        "written", ["Level 200", "level-200", "L200", "200", " LEVEL_200 "]
    )
    def test_the_ways_a_lecturer_actually_types_it(self, written):
        """They should not have to learn our slug format to fill in a form."""
        assert normalise(written) == "level_200"

    @pytest.mark.parametrize(
        "alias,expected",
        [("PhD", "doctoral"), ("MPhil", "masters"), ("MSc", "masters"),
         ("diploma", "hnd"), ("final year", "level_400")],
    )
    def test_common_aliases_resolve(self, alias, expected):
        assert normalise(alias) == expected

    def test_pilot_years_share_a_tier(self):
        """Level 100 and Level 200 are taught the same way — the pilot cohort."""
        assert tier_for("level_100") == tier_for("level_200") == "foundation"

    def test_the_ladder_climbs(self):
        assert tier_for("level_300") == "intermediate"
        assert tier_for("level_400") == "advanced"
        assert tier_for("masters") == "masters"
        assert tier_for("doctoral") == "doctoral"

    def test_professional_programmes_reach_level_600(self):
        """Medicine and Architecture run past the usual four years."""
        assert tier_for("level_500") == "advanced"
        assert tier_for("level_600") == "advanced"

    def test_hnd_is_foundation_but_applied(self):
        """Technical universities teach practice — a worked example matters
        more than a derivation."""
        block = scaffolding_for("hnd")
        assert "FOUNDATION" in block
        assert "applied" in block.lower()

    def test_level_100_is_not_given_the_hnd_emphasis(self):
        assert "HND / Diploma cohort" not in scaffolding_for("level_100")

    def test_foundation_and_doctoral_teach_differently(self):
        assert "every step" in scaffolding_for("level_100")
        assert "Do not explain standard results" in scaffolding_for("doctoral")

    def test_level_lands_in_the_cacheable_system_text(self):
        """A course's level does not change between turns."""
        system, user = build_system_and_user(
            user_message="q", context=["c"], mode="learn", academic_level="doctoral"
        )
        assert "LEVEL: DOCTORAL" in system
        assert "LEVEL: DOCTORAL" not in user

    def test_absent_level_leaves_the_prompt_unchanged(self):
        with_none, _ = build_system_and_user(
            user_message="q", context=["c"], mode="learn", academic_level=None
        )
        plain, _ = build_system_and_user(user_message="q", context=["c"], mode="learn")
        assert with_none == plain

    def test_the_db_constraint_and_the_code_agree(self):
        """A level the code accepts but the CHECK rejects is a 500 at save."""
        from pathlib import Path

        sql = (Path(__file__).resolve().parents[2]
               / "sql" / "012_use_ghanaian_academic_levels.sql").read_text(encoding="utf-8")
        for code in LEVEL_CODES:
            assert f"'{code}'::text" in sql


class TestRetryBudget:
    """The rewrite is a second model call plus a second full retrieval. It
    measured as the largest contributor to slow turns (9.3s vs a 3.3s best),
    so it gets the same treatment as re-ranking: a hard ceiling, then the
    first retrieval stands."""

    def test_a_budget_exists_and_is_short(self):
        assert 0.5 <= crag.RETRY_BUDGET <= 5.0

    def test_it_is_tunable(self, monkeypatch):
        import importlib
        monkeypatch.setenv("CRAG_RETRY_BUDGET_MS", "3500")
        importlib.reload(crag)
        assert crag.RETRY_BUDGET == 3.5
        monkeypatch.delenv("CRAG_RETRY_BUDGET_MS")
        importlib.reload(crag)

    def test_the_service_enforces_it(self):
        from pathlib import Path
        import app.ai.rag.service as svc

        src = Path(svc.__file__).read_text(encoding="utf-8")
        assert "asyncio.wait_for" in src
        assert "crag.RETRY_BUDGET" in src

    def test_a_timeout_keeps_the_first_retrieval(self):
        """Not an error path — the first retrieval is already an answer."""
        from pathlib import Path
        import app.ai.rag.service as svc

        src = Path(svc.__file__).read_text(encoding="utf-8")
        block = src[src.index("except asyncio.TimeoutError"):]
        assert "outcome = None" in block[:400]


class TestRetrievalDeadline:
    """Per-stage budgets compound. A turn that hit both the re-rank ceiling
    (2.5s) and the CRAG ceiling (2.0s) waited 4.5s and discarded BOTH results
    — an 8.6s TTFT produced by caps meant to protect it. What a student
    experiences is the total, so the total is what is bounded."""

    def test_a_single_deadline_exists(self):
        import app.ai.rag.service as svc

        assert 2.0 <= svc.RETRIEVAL_DEADLINE <= 10.0

    def test_the_deadline_covers_the_whole_phase(self):
        from pathlib import Path
        import app.ai.rag.service as svc

        src = Path(svc.__file__).read_text(encoding="utf-8")
        # started before the first retrieval, not after it
        assert src.index("deadline = time.monotonic()") < src.index("chunks = await retrieve(")

    def test_the_retry_gets_only_the_time_left(self):
        from pathlib import Path
        import app.ai.rag.service as svc

        src = Path(svc.__file__).read_text(encoding="utf-8")
        assert "min(crag.RETRY_BUDGET, remaining)" in src

    def test_a_retry_is_not_started_without_room(self):
        """Starting with a sliver guarantees a timeout the student pays for
        and gains nothing."""
        from pathlib import Path
        import app.ai.rag.service as svc

        src = Path(svc.__file__).read_text(encoding="utf-8")
        assert "can_retry" in src
        assert "crag.MIN_RETRY_WINDOW" in src

    def test_the_min_window_is_smaller_than_the_budget(self):
        assert crag.MIN_RETRY_WINDOW < crag.RETRY_BUDGET

    def test_worst_case_is_bounded_by_the_deadline_not_the_sum(self):
        """The property that failed before: deadline < rerank + crag."""
        import app.ai.rag.service as svc
        from app.ai.rag import retriever as R

        assert svc.RETRIEVAL_DEADLINE < R._RERANK_BUDGET + crag.RETRY_BUDGET + 3.0
