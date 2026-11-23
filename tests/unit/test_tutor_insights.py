"""Tutor activity monitor, misconception radar and student timeline."""

import inspect
from datetime import datetime, timezone

from app.ai import tasks
from app.ai.rag.modes_sessions import service
from app.core import events
from app.core.tutor_insights import misconception_radar, student_timeline, tutor_activity

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def turn(level, *, asked=False, session="s1", user="u1", at="2026-09-25T10:00"):
    return {"user_id": user, "session_id": session, "created_at": at,
            "payload": {"help_level": level, "help_asked": asked}}


class TestTutorActivity:
    def test_counts_each_rung(self):
        r = tutor_activity([turn(0), turn(1), turn(3), turn(4), turn(5, asked=True)])
        c = r["counts"]
        assert (c["concept_explanations"], c["socratic_prompts"], c["structural_hints"],
                c["partial_steps"], c["worked_solutions"], c["solutions_requested"]) == (1, 1, 1, 1, 1, 1)

    def test_unasked_solution_without_a_hint_is_the_tutor_skipping_the_ladder(self):
        skipped = [turn(5, at="2026-09-25T10:00")]
        laddered = [turn(2, session="s2", at="2026-09-25T10:00"), turn(5, session="s2", at="2026-09-25T10:05")]
        requested = [turn(5, asked=True, session="s3")]
        assert tutor_activity(skipped + laddered + requested)["sessions_skipping_ladder"] == 1

    def test_frequent_requesters_named_only_past_the_threshold(self):
        many = [turn(5, asked=True, user="u1")] * 5 + [turn(5, asked=True, user="u2")] * 2
        assert tutor_activity(many)["frequent_requesters"] == [{"student_id": "u1", "requests": 5}]

    def test_untagged_turns_are_ignored(self):
        r = tutor_activity([{"user_id": "u", "payload": {}}, {"user_id": "u", "payload": {"help_level": "x"}}])
        assert sum(r["counts"].values()) == 0


def detection(user, label, topic="Limits", at="2026-09-24T09:00"):
    return {"user_id": user, "topic": topic, "created_at": at, "payload": {"label": label}}


class TestMisconceptionRadar:
    def test_shared_error_is_grouped_case_insensitively(self):
        r = misconception_radar([
            detection("a", "Adds exponents when multiplying"),
            detection("b", "adds exponents when multiplying"),
            detection("b", "adds exponents when multiplying"),
        ], NOW)
        (m,) = r["misconceptions"]
        assert m["students"] == 2 and m["occurrences"] == 3 and m["emerging"]

    def test_one_students_error_is_not_a_class_matter(self):
        r = misconception_radar([detection("a", "x"), detection("a", "x")], NOW)
        assert r["misconceptions"] == [] and r["individual"] == 1

    def test_same_label_in_different_topics_is_different(self):
        r = misconception_radar([detection("a", "x", "Limits"), detection("b", "x", "Integrals")], NOW)
        assert r["misconceptions"] == []

    def test_old_misconception_is_not_emerging_and_wider_ranks_first(self):
        old = [detection(u, "old", at="2026-09-01T00:00") for u in "abc"]
        new = [detection(u, "new") for u in "ab"]
        labels = [(m["label"], m["emerging"]) for m in misconception_radar(new + old, NOW)["misconceptions"]]
        assert labels == [("old", False), ("new", True)]


def mastery_event(score, at, topic="Limits", source="review"):
    return {"event_type": "mastery.updated", "topic": topic, "created_at": at,
            "payload": {"score": score, "source": source}}


class TestStudentTimeline:
    def test_band_changes_become_mastered_slipped_recovered(self):
        kinds = [e["kind"] for e in student_timeline([
            mastery_event(0.5, "1"), mastery_event(0.75, "2"), mastery_event(0.6, "3"),
            mastery_event(0.8, "4"),
        ])]
        assert kinds == ["started", "mastered", "slipped", "recovered"]

    def test_steady_attempts_add_no_noise(self):
        assert len(student_timeline([mastery_event(0.5, "1"), mastery_event(0.55, "2")])) == 1

    def test_learn_session_is_one_line_summarising_the_help(self):
        rows = [dict(turn(2), event_type="turn.completed"), dict(turn(3), event_type="turn.completed"),
                dict(turn(5, asked=True), event_type="turn.completed"), dict(turn(0), event_type="turn.completed")]
        (entry,) = student_timeline(rows)
        assert entry["text"] == "Worked problems with the tutor: 2 hints and 1 full solution, 1 asked for."

    def test_misconception_and_exam_appear_in_order(self):
        rows = [
            {"event_type": "assessment.submitted", "created_at": "2", "payload": {"answered": 10}},
            {"event_type": "misconception.detected", "topic": "Limits", "created_at": "1",
             "payload": {"label": "divides by zero"}},
        ]
        assert [e["kind"] for e in student_timeline(rows)] == ["misconception", "exam"]


class TestMisconceptionCapture:
    def test_correct_answers_never_carry_a_misconception(self):
        assert service.misconception_label({"verdict": "correct", "misconception": "x"}) == ""
        assert service.misconception_label({"verdict": "partial", "misconception": "  sign  error. "}) == "sign error"
        assert service.misconception_label({"verdict": "incorrect"}) == ""

    def test_known_labels_are_offered_to_the_grader(self):
        assert service._known_misconceptions_block([]) == ""
        assert "- sign error" in service._known_misconceptions_block(["sign error"])

    def test_it_is_a_server_event_a_client_cannot_forge(self):
        assert events.MISCONCEPTION_DETECTED in events.SERVER_EVENTS
        assert events.MISCONCEPTION_DETECTED not in events.CLIENT_EVENTS

    def test_review_turn_emits_it(self):
        src = inspect.getsource(tasks._do_mode_session_turn)
        assert "events.MISCONCEPTION_DETECTED" in src and "known_misconceptions=" in src

    def test_the_helper_is_imported_where_it_is_called(self):
        """Found in the browser: the import landed in the session-START task, so
        every Review answer died with NameError. A text check cannot see that;
        the function's own bound names can."""
        assert "misconception_label" in tasks._do_mode_session_turn.__code__.co_varnames
