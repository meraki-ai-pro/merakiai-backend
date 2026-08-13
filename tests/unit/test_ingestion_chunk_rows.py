"""Chunk bookkeeping must never fail a completed ingestion.

These rows are written *after* the vectors are already live in Pinecone, so an
error here leaves a document serving queries while marked `failed` — the worst
of both states. It is also where the stale `document_chunks.mode` check
constraint bites, since it predates the 'application' mode the rest of the
schema uses.
"""

import pytest

from app.ai.ingestion.service import _CHECK_VIOLATION, _store_chunk_rows


class FakeTable:
    def __init__(self, recorder, failures):
        self.recorder = recorder
        self.failures = failures

    def insert(self, rows):
        self.recorder.append(rows)
        if self.failures:
            error = self.failures.pop(0)
            if error:
                raise error
        return self

    def execute(self):
        return self


class FakeSupabase:
    def __init__(self, failures=None):
        self.inserted = []
        self.failures = list(failures or [])

    def table(self, _name):
        return FakeTable(self.inserted, self.failures)


def rows(mode="application", n=2):
    return [{"document_id": "d1", "pinecone_id": f"p{i}", "mode": mode} for i in range(n)]


def constraint_error():
    return Exception(
        f'{{"message": "violates check constraint \\"document_chunks_mode_check\\"", '
        f'"code": "{_CHECK_VIOLATION}"}}'
    )


class TestHappyPath:
    def test_rows_are_written_once(self):
        sb = FakeSupabase()
        _store_chunk_rows(sb, rows(), "application", "notes.docx")
        assert len(sb.inserted) == 1

    def test_mode_is_left_alone_when_the_insert_succeeds(self):
        """Once the migration is applied the shim must never rewrite the value."""
        sb = FakeSupabase()
        _store_chunk_rows(sb, rows(), "application", "notes.docx")
        assert {r["mode"] for r in sb.inserted[0]} == {"application"}


class TestLegacyConstraint:
    def test_retries_with_the_value_the_constraint_accepts(self):
        sb = FakeSupabase(failures=[constraint_error(), None])
        _store_chunk_rows(sb, rows(), "application", "notes.docx")
        assert len(sb.inserted) == 2
        assert {r["mode"] for r in sb.inserted[1]} == {"practice"}

    def test_other_fields_survive_the_retry(self):
        sb = FakeSupabase(failures=[constraint_error(), None])
        _store_chunk_rows(sb, rows(), "application", "notes.docx")
        assert sb.inserted[1][0]["pinecone_id"] == "p0"
        assert sb.inserted[1][0]["document_id"] == "d1"

    def test_no_retry_for_a_mode_with_no_legacy_alias(self):
        sb = FakeSupabase(failures=[constraint_error()])
        _store_chunk_rows(sb, rows("learn"), "learn", "notes.docx")
        assert len(sb.inserted) == 1


class TestNeverFailsTheIngestion:
    def test_unrelated_error_is_swallowed(self):
        """The vectors are already live; raising here would mark the document
        failed while it is serving queries."""
        sb = FakeSupabase(failures=[Exception("connection reset")])
        _store_chunk_rows(sb, rows(), "application", "notes.docx")  # must not raise

    def test_failure_on_both_attempts_is_swallowed(self):
        sb = FakeSupabase(failures=[constraint_error(), Exception("still broken")])
        _store_chunk_rows(sb, rows(), "application", "notes.docx")  # must not raise

    def test_warns_rather_than_raising(self, caplog):
        sb = FakeSupabase(failures=[Exception("connection reset")])
        with caplog.at_level("WARNING"):
            _store_chunk_rows(sb, rows(), "application", "notes.docx")
        assert "vectors are live" in caplog.text

    def test_retry_path_names_the_migration(self, caplog):
        sb = FakeSupabase(failures=[constraint_error(), None])
        with caplog.at_level("WARNING"):
            _store_chunk_rows(sb, rows(), "application", "notes.docx")
        assert "001_allow_application_mode_in_document_chunks.sql" in caplog.text
