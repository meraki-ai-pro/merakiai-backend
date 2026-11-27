"""Exams: marking, imports, deadlines, and keeping the answer key server-side."""

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.api.v1 import assessments as exams
from app.api.v1.assessments import (
    AssessmentCreate, QuestionCreate, coerce_imported_question, deadline_for,
    effective_minutes, grade_objective, slice_for_import,
)


class TestObjectiveMarking:
    def test_mcq_ignores_case_spacing_and_edge_punctuation(self):
        assert grade_objective("mcq", "x = 2", "  X = 2. ")
        assert not grade_objective("mcq", "x = 2", "x = 3")

    def test_fill_blank_accepts_any_listed_alternative(self):
        assert grade_objective("fill_blank", "photosynthesis|photo-synthesis", "Photo-synthesis")

    def test_numbers_compare_as_numbers(self):
        assert grade_objective("fill_blank", "0.5", ".5")
        assert grade_objective("fill_blank", "1,000", "1000")
        assert not grade_objective("fill_blank", "0.5", "0.51")

    def test_latex_delimiters_in_an_imported_key_are_ignored(self):
        """Found live: the importer keyed a blank as "$2x$"."""
        assert grade_objective("fill_blank", "$2x$", "2x")
        assert grade_objective("fill_blank", "$0.5$", ".5")
        assert grade_objective("mcq", "$2$", "$2$")

    def test_blanks_mark_algebra_by_value_not_spelling(self):
        assert grade_objective("fill_blank", "2x+1", "1 + 2x")
        assert grade_objective("fill_blank", r"$\frac{1}{2}x^2$", "x^2/2")
        assert grade_objective("fill_blank", "y = 3x - 2", "y=-2+3*x")
        assert not grade_objective("fill_blank", "2x+1", "2x-1")

    def test_algebra_check_rejects_unsafe_or_non_maths_input(self):
        """parse_expr is eval underneath; a student's answer is untrusted."""
        assert not grade_objective("fill_blank", "1", "__import__('os').getcwd()")
        assert not grade_objective("fill_blank", "1", "9^9^9^9")
        assert not grade_objective("fill_blank", "mean", "mode")

    def test_blank_is_never_correct(self):
        assert not grade_objective("fill_blank", "", "")
        assert not grade_objective("mcq", "A", "   ")


class TestQuestionValidation:
    def test_mcq_key_must_be_an_option(self):
        with pytest.raises(ValidationError):
            QuestionCreate(prompt="?", options=["a", "b"], correct_answer="c")

    def test_short_answer_drops_options(self):
        q = QuestionCreate(question_type="short_answer", prompt="Explain", options=["x"], correct_answer="model")
        assert q.options == []

    def test_pre_post_can_no_longer_be_created(self):
        with pytest.raises(ValidationError):
            AssessmentCreate(course_id="c", kind="pre", title="t")
        assert AssessmentCreate(course_id="c", kind="midsem", title="t").kind == "midsem"

    def test_window_must_be_ordered(self):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            AssessmentCreate(course_id="c", kind="test", title="t", opens_at=now, closes_at=now)


class TestImportCoercion:
    def test_letter_keys_map_to_option_text(self):
        q = coerce_imported_question({"prompt": "2+2?", "options": ["3", "4"], "correct_answer": "B"})
        assert q["correct_answer"] == "4" and q["answer_source"] == "paper"

    def test_an_answer_not_among_the_options_is_blanked_for_the_lecturer(self):
        q = coerce_imported_question({"prompt": "?", "options": ["a", "b"], "correct_answer": "zebra"})
        assert q["correct_answer"] == "" and q["answer_source"] == "missing"

    def test_inferred_answers_stay_flagged(self):
        q = coerce_imported_question({"prompt": "?", "question_type": "short_answer",
                                      "correct_answer": "m", "answer_source": "inferred"})
        assert q["answer_source"] == "inferred"

    def test_type_is_guessed_from_the_shape(self):
        assert coerce_imported_question({"prompt": "?", "options": ["a", "b"]})["question_type"] == "mcq"
        assert coerce_imported_question({"prompt": "?"})["question_type"] == "short_answer"

    def test_no_prompt_no_question(self):
        assert coerce_imported_question({"prompt": "  "}) is None
        assert coerce_imported_question("text") is None

    def test_slices_break_between_questions(self):
        text = "\n\n".join(f"Q{i}. " + "x" * 50 for i in range(10))
        parts = slice_for_import(text, limit=120)
        assert len(parts) > 1
        assert all(p.strip().startswith("Q") for p in parts)
        assert "".join(parts).count("Q") == 10


class TestTiming:
    def test_extra_time_adds_minutes(self):
        assert effective_minutes(60, 15) == 75
        assert effective_minutes(60, 0) == 60
        assert effective_minutes(None, 50) is None

    def test_deadline_is_from_the_server_start(self):
        start = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
        assert deadline_for(start, 30, 15) == start + timedelta(minutes=45)


class TestAnswerKeyNeverReachesAStudent:
    def test_take_never_selects_the_key(self):
        source = inspect.getsource(exams.take)
        selects = [
            node.args[0].value
            for node in ast.walk(ast.parse(source.strip()))
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "select"
            and node.args and isinstance(node.args[0], ast.Constant)
        ]
        question_select = [s for s in selects if "prompt" in s]
        assert question_select and all("correct_answer" not in s and s != "*" for s in question_select)

    def test_exam_submission_returns_no_score(self):
        source = inspect.getsource(exams.submit)
        assert '"released": False' in source

    def test_result_is_gated_on_release(self):
        source = inspect.getsource(exams.my_result)
        assert source.index("results_released") < source.index("correct_answer")


class TestMixedPaperInsert:
    @pytest.mark.asyncio
    async def test_every_attempt_row_has_the_same_keys(self, monkeypatch):
        """PostgREST fills keys missing from some rows of a bulk insert with
        NULL, not the default — found live: mixed papers 500'd on NOT NULL."""
        questions = [
            {"id": "m", "question_type": "mcq", "correct_answer": "a", "points": 1, "topic": None},
            {"id": "s", "question_type": "short_answer", "correct_answer": "model", "points": 2, "topic": None},
        ]
        monkeypatch.setattr(exams, "_prepare_submission",
                            lambda *_a: ({"course_id": "c", "kind": "test"}, questions))

        async def fake_mark(q, a):
            return 1.0, "ok", True

        monkeypatch.setattr(exams, "suggest_mark", fake_mark)
        inserted = {}

        class Table:
            def insert(self, rows):
                inserted["rows"] = rows
                return self

            def execute(self):
                return type("R", (), {"data": []})()

        monkeypatch.setattr(exams, "get_supabase", lambda: type("S", (), {"table": lambda s, n: Table()})())
        monkeypatch.setattr(exams.mastery, "record_attempt", lambda **_k: None)
        monkeypatch.setattr(exams.events, "emit", lambda *a, **k: None)

        payload = exams.Submission(answers=[
            exams.SubmissionItem(question_id="m", answer="a"),
            exams.SubmissionItem(question_id="s", answer="x"),
        ])
        await exams.submit("p", payload, user={"id": "u"})
        rows = inserted["rows"]
        assert len({frozenset(r) for r in rows}) == 1, [sorted(r) for r in rows]
        assert all(r["needs_review"] is not None for r in rows)

    @pytest.mark.asyncio
    async def test_a_failed_suggestion_leaves_no_student_visible_note(self, monkeypatch):
        """Found live: 'please mark by hand' was stored as ai_feedback, which a
        student reads once results are released."""
        questions = [{"id": "s", "question_type": "short_answer", "correct_answer": "m", "points": 2, "topic": None}]
        monkeypatch.setattr(exams, "_prepare_submission", lambda *_a: ({"course_id": "c", "kind": "test"}, questions))

        async def failed(q, a):
            return 0.0, "Automatic marking was unavailable — please mark by hand.", False

        monkeypatch.setattr(exams, "suggest_mark", failed)
        inserted = {}

        class Table:
            def insert(self, rows):
                inserted["rows"] = rows
                return self

            def execute(self):
                return type("R", (), {"data": []})()

        monkeypatch.setattr(exams, "get_supabase", lambda: type("S", (), {"table": lambda s, n: Table()})())
        monkeypatch.setattr(exams.mastery, "record_attempt", lambda **_k: None)
        monkeypatch.setattr(exams.events, "emit", lambda *a, **k: None)

        await exams.submit("p", exams.Submission(answers=[exams.SubmissionItem(question_id="s", answer="x")]), user={"id": "u"})
        row = inserted["rows"][0]
        assert row["ai_feedback"] is None and row["ai_score"] is None and row["needs_review"] is True


class TestSuggestedMarks:
    @pytest.mark.asyncio
    async def test_suggestion_is_clamped_and_rounded(self, monkeypatch):
        async def fake(**_kw):
            return '{"score": 7.3, "feedback": "Good"}'

        monkeypatch.setattr("app.ai.rag.claude.generate_response", fake)
        score, feedback, ok = await exams.suggest_mark(
            {"id": "q", "prompt": "p", "correct_answer": "m", "points": 5}, "an answer"
        )
        assert (score, feedback, ok) == (5.0, "Good", True)

    @pytest.mark.asyncio
    async def test_a_model_failure_leaves_it_for_the_lecturer(self, monkeypatch):
        async def boom(**_kw):
            raise RuntimeError("overloaded")

        monkeypatch.setattr("app.ai.rag.claude.generate_response", boom)
        score, _, ok = await exams.suggest_mark(
            {"id": "q", "prompt": "p", "correct_answer": "m", "points": 5}, "an answer"
        )
        assert score == 0 and ok is False

    @pytest.mark.asyncio
    async def test_blank_answer_costs_no_model_call(self, monkeypatch):
        async def never(**_kw):
            raise AssertionError("called")

        monkeypatch.setattr("app.ai.rag.claude.generate_response", never)
        assert (await exams.suggest_mark({"id": "q", "prompt": "p", "correct_answer": "m", "points": 2}, " "))[0] == 0


class TestExamResultsExport:
    QS = [{"id": "q1", "points": 2}, {"id": "q2", "points": 3}]
    ATTEMPTS = [
        {"student_id": "a", "question_id": "q1", "score": 2, "needs_review": False, "created_at": "2026-10-01T09:00:00"},
        {"student_id": "a", "question_id": "q2", "score": 3, "needs_review": False, "created_at": "2026-10-01T09:05:00"},
        {"student_id": "b", "question_id": "q1", "score": 1, "needs_review": False, "created_at": "2026-10-01T10:00:00"},
        {"student_id": "b", "question_id": "q2", "score": 0, "needs_review": True, "created_at": "2026-10-01T10:02:00"},
    ]

    def test_stats_from_tally(self):
        total, by_student = exams.tally(self.QS, self.ATTEMPTS)
        stats = exams.exam_stats(total, by_student)
        assert total == 5 and by_student["a"]["submitted_at"] == "2026-10-01T09:05:00"
        assert (stats["mean_percent"], stats["highest_percent"], stats["lowest_percent"]) == (60.0, 100.0, 20.0)
        assert stats["pass_rate"] == 50.0 and stats["pending_review"] == 1
        assert sum(b["students"] for b in stats["distribution"]) == 2

    def test_an_instructor_pass_mark_moves_the_pass_rate(self):
        total, by_student = exams.tally(self.QS, self.ATTEMPTS)  # students on 100% and 20%
        assert exams.exam_stats(total, by_student, pass_mark=10)["pass_rate"] == 100.0
        assert exams.exam_stats(total, by_student, pass_mark=100)["pass_rate"] == 50.0
        assert exams.pass_mark_of({"pass_mark": None}) == 50.0
        assert exams.pass_mark_of({"pass_mark": 40}) == 40.0

    def test_unsat_paper_has_no_invented_zeros(self):
        assert "mean_percent" not in exams.exam_stats(1.0, {})

    def test_workbook_has_a_sheet_per_exam_with_per_question_marks(self):
        from io import BytesIO

        from openpyxl import load_workbook

        total, by_student = exams.tally(self.QS, self.ATTEMPTS)
        paper = {"title": "Mid-sem: Limits/Derivatives", "kind": "midsem", "results_released": False}
        data = [{"paper": paper, "questions": self.QS, "total_points": total, "by_student": by_student}] * 2
        wb = load_workbook(BytesIO(exams.build_results_workbook("Calc", data, {"a": {"name": "Ama", "email": "a@x"}})))
        assert len(wb.sheetnames) == 3 and len(set(wb.sheetnames)) == 3
        ws = wb[wb.sheetnames[1]]
        assert [c.value for c in ws[2]][:5] == ["Ama", "a@x", 2, 3, 5]


class TestIntegrityReview:
    def test_adjustment_keeps_question_marks_and_clamps_total(self):
        class Query:
            def select(self, *_): return self
            def eq(self, *_): return self
            def execute(self):
                return type("Result", (), {"data": [{"student_id": "s", "deduction": 5,
                    "integrity_status": "confirmed", "feedback": "Unauthorised notes"}]})()
        sb = type("DB", (), {"table": lambda self, _: Query()})()
        rows = {"s": {"score": 3, "by_question": {"q": 3}}}
        exams.apply_integrity_reviews(sb, "exam", rows)
        assert rows["s"]["score"] == 0
        assert rows["s"]["raw_score"] == 3
        assert rows["s"]["by_question"] == {"q": 3}
        assert rows["s"]["integrity_feedback"] == "Unauthorised notes"

    def test_released_results_cannot_be_penalised(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setattr(exams, "_load_owned", lambda *_: {"results_released": True})
        with pytest.raises(HTTPException) as exc:
            exams.review_integrity("e", "s", exams.IntegrityReview(
                integrity_status="confirmed", deduction=1, feedback="Reason"), None, {"id": "teacher"})
        assert exc.value.status_code == 409

    def test_concern_alone_cannot_deduct_marks(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setattr(exams, "_load_owned", lambda *_: {})
        with pytest.raises(HTTPException) as exc:
            exams.review_integrity("e", "s", exams.IntegrityReview(
                integrity_status="concern", deduction=1, feedback="Reason"), None, {"id": "teacher"})
        assert exc.value.status_code == 400

    def test_feedback_is_hidden_until_release(self, monkeypatch):
        monkeypatch.setattr(exams, "_load_published_for_student", lambda *_: {"results_released": False})
        monkeypatch.setattr(exams, "get_supabase", lambda: pytest.fail("Must not query unreleased feedback"))
        assert exams.my_result("e", {"id": "s"}) == {"released": False}

    def test_non_finite_deduction_rejected(self):
        for value in (float("nan"), float("inf"), -1):
            with pytest.raises(ValidationError):
                exams.IntegrityReview(integrity_status="confirmed", deduction=value, feedback="Reason")

    def test_reuse_resets_schedule_audience_and_results(self, monkeypatch):
        source = {"id": "old", "course_id": "c", "kind": "test", "title": "Exam",
                  "is_published": True, "results_released": True, "target_student_ids": ["s"],
                  "opens_at": "2026-01-01", "closes_at": "2026-01-02", "time_limit_minutes": 60}
        monkeypatch.setattr(exams, "_load_owned", lambda *_: source)
        saved = {}
        def create(payload, *_):
            saved.update(payload.model_dump())
            return {"assessment": {"id": "new"}}
        monkeypatch.setattr(exams, "create_assessment", create)
        inserted = []
        class Query:
            def select(self, *_): return self
            def eq(self, *_): return self
            def insert(self, rows): inserted.extend(rows); return self
            def execute(self):
                return type("R", (), {"data": [{"id": "q", "assessment_id": "old", "prompt": "Question", "points": 2}]})()
        monkeypatch.setattr(exams, "get_supabase", lambda: type("DB", (), {"table": lambda self, _: Query()})())
        assert exams.reuse_assessment("old", None, {"id": "t"})["assessment"]["id"] == "new"
        assert saved["opens_at"] is None and saved["target_student_ids"] is None
        assert saved["time_limit_minutes"] == 60
        assert inserted == [{"assessment_id": "new", "prompt": "Question", "points": 2}]


class TestLatestStart:
    PAPER = {"time_limit_minutes": 120, "closes_at": "2026-11-27T14:00:00Z"}

    def test_each_student_has_their_own_full_countdown(self):
        a = datetime(2026, 11, 27, 10, 15, tzinfo=timezone.utc)
        b = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
        exams.check_start_allowed(self.PAPER, a, 0, None)
        exams.check_start_allowed(self.PAPER, b, 0, None)
        assert deadline_for(a, 120, 0).hour == 12
        assert deadline_for(a, 120, 0).minute == 15
        assert deadline_for(b, 120, 0).hour == 14

    def test_no_admission_after_latest_start(self):
        from fastapi import HTTPException
        cutoff = exams.latest_start_for(self.PAPER)
        assert cutoff == datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        with pytest.raises(HTTPException) as error:
            exams.check_start_allowed(self.PAPER, cutoff + timedelta(microseconds=1), 0, None)
        assert error.value.status_code == 403

    def test_resume_after_cutoff_preserves_original_deadline(self):
        started = datetime(2026, 11, 27, 10, 15, tzinfo=timezone.utc)
        now = datetime(2026, 11, 27, 12, 5, tzinfo=timezone.utc)
        exams.check_start_allowed(self.PAPER, now, 0, started)
        assert deadline_for(started, 120, 0) == datetime(2026, 11, 27, 12, 15, tzinfo=timezone.utc)

    def test_accommodations_require_earlier_admission(self):
        assert exams.latest_start_for(self.PAPER, 30) == datetime(2026, 11, 27, 11, 30, tzinfo=timezone.utc)

    def test_no_cutoff_for_untimed_or_open_ended_papers(self):
        assert exams.latest_start_for({"closes_at": self.PAPER["closes_at"]}) is None
        assert exams.latest_start_for({"time_limit_minutes": 120}) is None
