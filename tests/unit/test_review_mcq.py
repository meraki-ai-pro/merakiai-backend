"""Review MCQs: the key must name a real option, and the order is shuffled."""

import random
from collections import Counter

from app.ai.rag.modes_sessions.service import mcq_choice, shuffle_mcq

ITEM = {"question": "2 + 2?", "options": {"A": "4", "B": "3", "C": "5", "D": "22"}, "correct": "A"}


def test_key_follows_the_answer_through_the_shuffle():
    for seed in range(20):
        out = shuffle_mcq(ITEM, random.Random(seed))
        assert out["options"][out["correct"]] == "4"
        assert sorted(out["options"].values()) == sorted(ITEM["options"].values())


def test_correct_letter_is_spread_across_positions():
    letters = Counter(shuffle_mcq(ITEM, random.Random(s))["correct"] for s in range(400))
    assert set(letters) == {"A", "B", "C", "D"}
    assert min(letters.values()) > 60  # ~100 each; "always A" would fail


def test_question_without_a_valid_key_is_rejected():
    assert shuffle_mcq({**ITEM, "correct": "E"}) is None
    assert shuffle_mcq({**ITEM, "correct": ""}) is None
    assert shuffle_mcq({k: v for k, v in ITEM.items() if k != "correct"}) is None
    assert shuffle_mcq({**ITEM, "options": {"A": "4", "B": "4", "C": "5", "D": "6"}}) is None


def test_lowercase_keys_and_bracketed_key_are_accepted():
    out = shuffle_mcq({**ITEM, "options": {"a": "4", "b": "3"}, "correct": "(a)"}, random.Random(1))
    assert out["options"][out["correct"]] == "4"


def test_reading_the_students_choice():
    opts = {"A": "Bananas", "B": "3", "C": "5", "D": "22"}
    assert mcq_choice("B", opts) == "B"
    assert mcq_choice("b) 3", opts) == "B"
    assert mcq_choice("(C)", opts) == "C"
    assert mcq_choice("bananas", opts) == "A"  # option text beats first letter
    assert mcq_choice("because it is 5", opts) is None
    assert mcq_choice("E", opts) is None


def test_exam_options_are_stable_per_student_and_differ_between_students():
    from app.api.v1.assessments import shuffled_options

    opts = ["4", "3", "5", "22"]
    a = shuffled_options(opts, "exam", "student-a", "q1")
    assert a == shuffled_options(opts, "exam", "student-a", "q1")  # reload keeps order
    assert sorted(a) == sorted(opts)
    orders = {tuple(shuffled_options(opts, "exam", f"s{i}", "q1")) for i in range(30)}
    assert len(orders) > 5


def test_question_order_is_per_student_and_keeps_every_question():
    from app.api.v1.assessments import shuffled_options

    questions = [{"id": f"q{i}"} for i in range(10)]
    a = shuffled_options(questions, "exam", "student-a")
    assert a == shuffled_options(questions, "exam", "student-a")  # reopening keeps the order
    assert sorted(q["id"] for q in a) == sorted(q["id"] for q in questions)
    assert a != shuffled_options(questions, "exam", "student-b")


def test_integrity_summary_counts_by_kind():
    from app.api.v1.assessments import integrity_summary

    events = [{"kind": "left_window"}, {"kind": "paste"}, {"kind": "left_window"}]
    assert integrity_summary(events) == {"left_window": 2, "paste": 1}
    assert integrity_summary([]) == {}
