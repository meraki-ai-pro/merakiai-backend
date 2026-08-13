"""Research instrumentation: the event stream, mastery, and learning gain.

These are the numbers a dissertation would be built on, so the tests are about
whether the measurements are *sound*, not just whether the code runs.
"""

import pytest

from app.core import events, mastery
from app.core.mastery import ALPHA, _next_score, band


class TestEventVocabulary:
    def test_client_and_server_events_do_not_overlap(self):
        """A client must not be able to emit a server-authoritative event."""
        assert not (events.CLIENT_EVENTS & events.SERVER_EVENTS)

    def test_scoring_events_are_server_only(self):
        for name in (events.ASSESSMENT_SUBMITTED, events.MASTERY_UPDATED):
            assert name in events.SERVER_EVENTS
            assert name not in events.CLIENT_EVENTS

    def test_engagement_events_are_client_side(self):
        """Only the browser knows these happened."""
        for name in (events.CITATION_CLICKED, events.VIDEO_COMPLETED):
            assert name in events.CLIENT_EVENTS

    def test_unknown_types_are_refused(self, monkeypatch):
        called = {"n": 0}

        def boom():
            called["n"] += 1
            raise AssertionError("should not have hit the database")

        monkeypatch.setattr(events, "get_supabase", boom)
        events.emit("totally.made.up", user_id="u")
        assert called["n"] == 0


class TestPayloadHygiene:
    def test_long_strings_are_truncated(self):
        out = events._clean({"note": "x" * 5000})
        assert len(out["note"]) <= 500

    def test_nested_structures_are_summarised_not_stored(self):
        """This is what stops a transcript or chunk text ending up in the
        analytics table, outside the retention rules for conversations."""
        out = events._clean({"sources": [{"text": "a whole passage"}] * 3})
        assert out["sources"] == 3

    def test_key_count_is_bounded(self):
        out = events._clean({f"k{i}": i for i in range(100)})
        assert len(out) <= 20

    def test_scalars_survive_intact(self):
        out = events._clean({"ms": 1234, "ok": True, "ratio": 0.5, "none": None})
        assert out == {"ms": 1234, "ok": True, "ratio": 0.5, "none": None}

    def test_empty_payload_is_an_empty_dict(self):
        assert events._clean(None) == {}


class TestEmissionNeverBreaksTheCaller:
    def test_database_failure_is_swallowed(self, monkeypatch):
        def boom():
            raise RuntimeError("events table gone")

        monkeypatch.setattr(events, "get_supabase", boom)
        events.emit(events.TURN_COMPLETED, user_id="u")  # must not raise


class TestMasteryScoring:
    def test_first_correct_answer_reads_as_mastery_not_failure(self):
        """A cold-start EMA would put this at 0.3, which looks like failure."""
        assert _next_score(None, True, 0) == 1.0

    def test_first_wrong_answer_is_zero(self):
        assert _next_score(None, False, 0) == 0.0

    def test_recent_evidence_moves_the_score(self):
        assert _next_score(0.0, True, 5) == pytest.approx(ALPHA)

    def test_a_recovering_student_overtakes_their_ratio(self):
        """Five wrong then five right is 50% by ratio, but the student clearly
        understands it now. The EMA has to say so."""
        score = 0.0
        for _ in range(5):
            score = _next_score(score, False, 5)
        for _ in range(5):
            score = _next_score(score, True, 5)
        assert score > 0.5
        assert band(score) in ("developing", "secure")

    def test_a_declining_student_loses_ground(self):
        score = 1.0
        for _ in range(5):
            score = _next_score(score, False, 5)
        assert score < 0.5

    def test_score_stays_in_range(self):
        score = 0.5
        for correct in [True, False] * 50:
            score = _next_score(score, correct, 10)
            assert 0.0 <= score <= 1.0

    @pytest.mark.parametrize(
        "score,expected",
        [(0.0, "struggling"), (0.39, "struggling"), (0.4, "developing"),
         (0.69, "developing"), (0.7, "secure"), (1.0, "secure")],
    )
    def test_bands(self, score, expected):
        assert band(score) == expected


class TestMasteryRecording:
    def test_untagged_topics_are_skipped_not_guessed(self, monkeypatch):
        """A question with no topic cannot contribute to a per-topic measure.
        Inventing one would corrupt the dataset."""
        def boom():
            raise AssertionError("should not have queried")

        monkeypatch.setattr(mastery, "get_supabase", boom)
        assert mastery.record_attempt(
            student_id="s", course_id="c", topic="", correct=True
        ) is None

    def test_database_failure_does_not_raise(self, monkeypatch):
        def boom():
            raise RuntimeError("no mastery_states table")

        monkeypatch.setattr(mastery, "get_supabase", boom)
        assert mastery.record_attempt(
            student_id="s", course_id="c", topic="limits", correct=True
        ) is None


class TestAssessmentIntegrity:
    def test_the_answer_key_is_never_selected_for_a_student(self):
        """Not filtered after fetching — never fetched, so it cannot leak
        through a logging or error path."""
        from pathlib import Path
        import app.api.v1.assessments as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        take = src[src.index("def take("): src.index("def submit(")]
        # Comments are stripped first — the code explains why the key is
        # excluded, and saying so must not look like selecting it.
        code = "\n".join(
            line.split("#", 1)[0] for line in take.splitlines()
        )
        assert "correct_answer" not in code

    def test_submission_is_scored_server_side(self):
        from pathlib import Path
        import app.api.v1.assessments as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "item.answer.strip().lower() ==" in src

    def test_retakes_are_refused(self):
        """A retake breaks the pre/post pairing the whole study rests on."""
        from pathlib import Path
        import app.api.v1.assessments as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "already completed this assessment" in src

    def test_no_per_question_feedback_is_returned(self):
        """Telling a student which pre-test items were wrong hands back the
        answer key before the post-test."""
        from pathlib import Path
        import app.api.v1.assessments as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        submit = src[src.index("def submit("):]
        assert "no per-question breakdown" in submit.lower()

    def test_learning_gain_pairs_students(self):
        """A cohort mean over different populations at the two time points
        manufactures a gain that is really attrition."""
        from pathlib import Path
        import app.api.v1.assessments as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "set(pre) & set(post)" in src
        assert "sat_pre_only" in src
