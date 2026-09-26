"""Tester round (2026-09-26): Review feeds mastery, and the Socratic help ladder."""

import inspect

import pytest

from app.ai import tasks
from app.ai.rag import help_ladder, prompt_builder
from app.ai.rag.modes_sessions import service
from app.core import mastery


class TestTopicVocabulary:
    def test_a_model_label_is_mapped_to_the_lecturers_spelling(self):
        assert mastery.match_topic("  limits ", ["Derivatives", "Limits"]) == "Limits"

    def test_an_invented_label_counts_towards_nothing(self):
        """"Limit laws" is not the lecturer's topic; a new mastery row for it
        would split one skill across rows that each look under-practised."""
        assert mastery.match_topic("Limit laws", ["Limits"]) == ""
        assert mastery.match_topic("", ["Limits"]) == ""

    def test_topic_list_failure_degrades_to_no_topics(self, monkeypatch):
        def boom():
            raise RuntimeError("down")
        monkeypatch.setattr(mastery, "get_supabase", boom)
        assert mastery.course_topics("c") == []

    def test_generator_is_given_the_topics_or_nothing(self):
        assert service._topic_block([]) == ""
        block = service._topic_block(["Limits", "Chain rule"])
        assert "- Limits" in block and "- Chain rule" in block

    @pytest.mark.asyncio
    async def test_generated_item_carries_the_matched_topic(self, monkeypatch):
        async def fake_retrieve(**_):
            return ["ref"]

        async def fake_generate(**_):
            return '{"type":"mcq","question":"q?","options":{"A":"1"},"correct_answer":"A","category":"limits"}'

        monkeypatch.setattr(service, "retrieve_context", fake_retrieve)
        monkeypatch.setattr(service, "generate_response", fake_generate)
        monkeypatch.setattr(service.mastery, "course_topics", lambda _: ["Limits"])
        item, _ = await service.generate_review_item("mcq", "Basic", "c", "Calc", {})
        assert item["topic"] == "Limits"

    def test_review_marking_records_mastery_as_review(self):
        src = inspect.getsource(tasks._do_mode_session_turn)
        assert "mastery.record_attempt" in src and 'source="review"' in src


class TestHelpLadder:
    @staticmethod
    def _stream(chunks):
        out = []
        f = help_ladder.TagFilter(out.append)
        for c in chunks:
            f(c)
        f.flush()
        return "".join(out)

    def test_tag_split_across_chunks_never_reaches_the_student(self):
        assert self._stream(["[[he", "lp:3]]", " What is u?"]) == " What is u?"

    def test_untagged_answers_stream_unchanged(self):
        assert self._stream(["The derivative", " is 2x"]) == "The derivative is 2x"
        assert self._stream(["[1] cited"]) == "[1] cited"

    def test_short_answer_held_in_buffer_is_released(self):
        assert self._stream(["[[h"]) == "[[h"

    def test_parse_reads_level_and_request(self):
        assert help_ladder.parse("[[help:5:asked]]\nFull") == (5, True, "Full")
        assert help_ladder.parse("[[help:2]] Hint") == (2, False, "Hint")
        assert help_ladder.parse("No tag") == (None, False, "No tag")

    def test_ladder_is_taught_in_learn_only(self):
        learn, _ = prompt_builder.build_system_and_user("q", ["c"], "learn", previous_help=2)
        app_sys, app_user = prompt_builder.build_system_and_user("q", ["c"], "application", previous_help=2)
        assert "SOCRATIC HELP LADDER" in learn
        assert "SOCRATIC HELP LADDER" not in app_sys and "LADDER STATE" not in app_user

    def test_previous_rung_reaches_the_next_turn(self):
        _, user = prompt_builder.build_system_and_user("q", ["c"], "learn", previous_help=3)
        assert "rung 3" in user

    def test_previous_level_lookup_failure_restarts_the_ladder(self, monkeypatch):
        def boom():
            raise RuntimeError("down")
        monkeypatch.setattr(tasks, "get_supabase", boom)
        assert tasks._previous_help_level("s", "u") is None


class TestAttentionFlags:
    from datetime import datetime, timezone
    NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)

    def _flags(self, **kw):
        from app.core.attention import flags_for_course
        base = dict(now=self.NOW, enrolled=["s1"], sessions=[{"user_id": "s1", "started_at": "2026-09-25T10:00:00+00:00"}],
                    mastery_events=[], turn_events=[], mastery_rows=[])
        base.update(kw)
        return flags_for_course(**base)

    def test_active_student_with_nothing_wrong_is_not_listed(self):
        assert self._flags() == {}

    def test_declining_mastery_despite_practice(self):
        evs = [{"user_id": "s1", "topic": "Limits", "created_at": f"2026-09-2{i}", "payload": {"score": s}}
               for i, s in enumerate([0.8, 0.7, 0.55, 0.45])]
        (flag,) = self._flags(mastery_events=evs)["s1"]
        assert flag["kind"] == "declining" and "80% to 45%" in flag["reason"]

    def test_one_bad_answer_is_not_a_decline(self):
        evs = [{"user_id": "s1", "topic": "Limits", "created_at": f"2026-09-2{i}", "payload": {"score": s}}
               for i, s in enumerate([0.8, 0.5])]
        assert self._flags(mastery_events=evs) == {}

    def test_dependency_needs_both_volume_and_share(self):
        turns = [{"user_id": "s1", "payload": {"help_level": lvl}} for lvl in [5, 5, 5, 5, 2, 0, 0]]
        rows = [{"student_id": "s1", "topic": "Limits", "mastery_score": 0.46, "attempts_count": 2}]
        (flag,) = self._flags(turn_events=turns, mastery_rows=rows)["s1"]
        assert flag["kind"] == "dependency"
        assert "4 of 5 problems" in flag["reason"] and "46%" in flag["reason"]
        assert self._flags(turn_events=turns[:3]) == {}

    def test_disengaged_only_if_previously_regular(self):
        old = [{"user_id": "s1", "started_at": f"2026-09-0{d}T09:00:00+00:00"} for d in (1, 3, 5)]
        (flag,) = self._flags(sessions=old)["s1"]
        assert flag["kind"] == "disengaged" and "20 days" in flag["reason"]
        assert self._flags(sessions=old[:1]) == {}

    def test_never_started_and_withdrawn_are_not_listed(self):
        assert self._flags(sessions=[]) == {}
        # s2 has history but is no longer enrolled.
        rows = [{"student_id": "s2", "topic": "Limits", "mastery_score": 0.1, "attempts_count": 9}]
        assert "s2" not in self._flags(mastery_rows=rows)

    def test_stuck_after_many_attempts_and_most_urgent_first(self):
        rows = [{"student_id": "s1", "topic": "Chain rule", "mastery_score": 0.2, "attempts_count": 6}]
        turns = [{"user_id": "s1", "payload": {"help_level": 5}}] * 5
        kinds = [f["kind"] for f in self._flags(mastery_rows=rows, turn_events=turns)["s1"]]
        assert kinds == ["dependency", "stuck"]
