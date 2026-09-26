"""Intervention Studio: targeted papers, drafted lessons, and who may see them."""

import asyncio

import pytest
from fastapi import HTTPException

from app.api.v1 import assessments
from app.api.v1.lecturer import interventions as studio

LECTURER = {"id": "lec", "role": "admin"}


class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload = db, table, "select", None

    def __getattr__(self, _name):  # eq, in_, select, order, limit ... all chain
        return lambda *a, **k: self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def execute(self):
        if self.op == "insert":
            if self.table in self.db.fail_insert:
                raise RuntimeError("column target_student_ids does not exist")
            rows = self.payload if isinstance(self.payload, list) else [self.payload]
            rows = [{"id": f"{self.table}-{len(self.db.inserted)}", **r} for r in rows]
            self.db.inserted.append((self.table, rows))
            return type("R", (), {"data": rows})()
        return type("R", (), {"data": self.db.rows.get(self.table, [])})()


class FakeDB:
    def __init__(self, rows=None, fail_insert=()):
        self.rows, self.fail_insert, self.inserted = rows or {}, set(fail_insert), []

    def table(self, name):
        return FakeQuery(self, name)

    def of(self, table):
        return [r for t, rows in self.inserted if t == table for r in rows]


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB(rows={"enrolments": [{"student_id": "s1"}, {"student_id": "s2"}]})
    monkeypatch.setattr(studio, "get_supabase", lambda: fake)
    monkeypatch.setattr(studio, "assert_course_owner", lambda *a: None)
    monkeypatch.setattr(studio.audit, "record", lambda **k: None)
    return fake


class TestTargetedVisibility:
    def test_untargeted_paper_is_for_everyone(self):
        assert assessments.visible_to({"target_student_ids": None}, "anyone")
        assert assessments.visible_to({}, "anyone")

    def test_targeted_paper_is_for_its_students_only(self):
        paper = {"target_student_ids": ["s1"]}
        assert assessments.visible_to(paper, "s1")
        assert not assessments.visible_to(paper, "s2")

    def test_another_students_paper_is_a_404(self, monkeypatch):
        paper = {"id": "a", "course_id": "c", "is_published": True, "target_student_ids": ["s1"]}
        monkeypatch.setattr(assessments, "get_supabase", lambda: FakeDB(rows={"assessments": [paper]}))
        monkeypatch.setattr(assessments, "require_enrolment", lambda *a, **k: None)
        with pytest.raises(HTTPException) as e:
            assessments._load_published_for_student("a", {"id": "s2"})
        assert e.value.status_code == 404
        assert assessments._load_published_for_student("a", {"id": "s1"}) is paper


class TestQuestionValidation:
    def test_only_auto_markable_valid_questions_survive_tagged(self):
        raw = [
            {"question_type": "mcq", "prompt": "d/dx x^2?", "options": ["2x", "x", "2", "x^2"], "correct_answer": "2x"},
            {"question_type": "mcq", "prompt": "bad key", "options": ["a", "b"], "correct_answer": "c"},
            {"question_type": "short_answer", "prompt": "explain", "correct_answer": "..."},
            {"question_type": "fill_blank", "prompt": "d/dx x^3 = ____", "correct_answer": "3x^2"},
            "not a question",
        ]
        kept = studio.valid_questions(raw, "Differentiation")
        assert [q["prompt"] for q in kept] == ["d/dx x^2?", "d/dx x^3 = ____"]
        assert all(q["topic"] == "Differentiation" for q in kept)


class TestPractice:
    def _run(self, db, monkeypatch, **body):
        async def fake_retrieve(**_):
            return ["ref"]

        async def fake_ask(*_a, **_k):
            return {"questions": [{"question_type": "fill_blank", "prompt": "x+x = ____", "correct_answer": "2x"}]}

        import app.ai.rag.retriever as retriever
        monkeypatch.setattr(retriever, "retrieve_context", fake_retrieve)
        monkeypatch.setattr(studio, "_ask_json", fake_ask)
        payload = studio.PracticeRequest(topic="Algebra", **body)
        return asyncio.run(studio.create_practice("c", payload, None, LECTURER))

    def test_diagnostic_is_a_timed_draft_for_the_chosen_active_students(self, db, monkeypatch):
        out = self._run(db, monkeypatch, format="diagnostic", student_ids=["s1", "outsider"])
        (paper,) = db.of("assessments")
        assert out["status"] == "draft" and "is_published" not in paper
        assert paper["target_student_ids"] == ["s1"]  # the outsider is dropped
        assert paper["time_limit_minutes"] == 10 and paper["kind"] == "quiz"
        assert db.of("assessment_questions")[0]["topic"] == "Algebra"

    def test_retrieval_check_opens_a_week_later(self, db, monkeypatch):
        self._run(db, monkeypatch, format="retrieval", student_ids=["s1"])
        (paper,) = db.of("assessments")
        assert paper["opens_at"] < paper["closes_at"]

    def test_no_active_students_is_refused(self, db, monkeypatch):
        db.rows["enrolments"] = []
        with pytest.raises(HTTPException) as e:
            self._run(db, monkeypatch, student_ids=["gone"])
        assert e.value.status_code == 400

    def test_without_migration_it_refuses_rather_than_publishing_to_everyone(self, db, monkeypatch):
        db.fail_insert.add("assessments")
        with pytest.raises(HTTPException) as e:
            self._run(db, monkeypatch, student_ids=["s1"])
        assert e.value.status_code == 503 and "017" in e.value.detail


class TestMiniLessonSend:
    def test_each_active_student_gets_a_session_opening_with_the_lesson(self, db):
        out = studio.send_mini_lesson(
            "c", studio.LessonSend(topic="Limits", lesson="A limit is ... " * 3, student_ids=["s1", "s2", "x"]),
            None, LECTURER,
        )
        assert out == {"status": "sent", "sent": 2, "skipped": 1}
        sessions, convos = db.of("sessions"), db.of("conversations")
        assert {s["user_id"] for s in sessions} == {"s1", "s2"}
        assert all(s["title"] == "From your lecturer: Limits" for s in sessions)
        assert all(c["user_input"].startswith("(system)") for c in convos)

    def test_empty_list_means_the_whole_active_class(self, db):
        out = studio.send_mini_lesson(
            "c", studio.LessonSend(topic="Limits", lesson="A limit is ... " * 3), None, LECTURER,
        )
        assert out["sent"] == 2 and out["skipped"] == 0
