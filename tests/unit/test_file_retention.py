"""Source-file retention and conversation source persistence.

Ref: TECHNICAL_DOCUMENTATION §25.1
     Meraki_AI_Lecturer_Side_Technical_Documentation §3.3

Both operations are deliberately best-effort. They run *after* the thing that
matters has already succeeded — the vectors are live, the answer is written —
so failing loudly here would destroy real work to protect an archive copy.
"""

import pytest

from app.media import storage_service as ss
from app.media.storage_service import (
    COURSE_DOCS_BUCKET,
    STUDENT_UPLOADS_BUCKET,
    _safe_segment,
    signed_url,
    upload_course_document,
    upload_student_image,
)


class FakeBucketApi:
    def __init__(self, recorder, fail=False):
        self.recorder = recorder
        self.fail = fail

    def upload(self, path, file, file_options):
        if self.fail:
            raise RuntimeError("storage unavailable")
        self.recorder.append({"path": path, "size": len(file), "options": file_options})
        return {"path": path}

    def create_signed_url(self, path, ttl):
        if self.fail:
            raise RuntimeError("storage unavailable")
        return {"signedURL": f"https://sig/{path}?exp={ttl}"}


class FakeStorage:
    def __init__(self, recorder, fail=False):
        self.recorder = recorder
        self.fail = fail
        self.buckets = []

    def from_(self, bucket):
        self.buckets.append(bucket)
        return FakeBucketApi(self.recorder, self.fail)


class FakeSupabase:
    def __init__(self, recorder, fail=False):
        self.storage = FakeStorage(recorder, fail)


@pytest.fixture
def storage(monkeypatch):
    recorder: list[dict] = []

    def _apply(fail=False):
        fake = FakeSupabase(recorder, fail)
        monkeypatch.setattr(ss, "get_supabase", lambda: fake)
        return recorder, fake

    return _apply


class TestPathSafety:
    @pytest.mark.parametrize(
        "raw",
        ["../../etc/passwd", "a/b/c.pdf", "x\\y.pdf", "..%2f..", "a\x00b/c"],
    )
    def test_no_separator_survives(self, raw):
        """The invariant that makes traversal impossible: the result is always
        exactly one path segment. A literal '..' with no separator around it is
        an inert filename, so it need not be stripped — but a '/' or '\\' would
        let an upload write outside its own folder."""
        out = _safe_segment(raw)
        assert "/" not in out and "\\" not in out

    @pytest.mark.parametrize("raw", [".", "..", "...", ".ssh"])
    def test_result_is_never_a_relative_reference_or_dotfile(self, raw):
        out = _safe_segment(raw)
        assert out not in (".", "..")
        assert not out.startswith(".")

    def test_control_characters_are_stripped(self):
        assert "\x00" not in _safe_segment("a\x00b.pdf")

    def test_empty_name_still_yields_a_segment(self):
        assert _safe_segment("") == "file"

    def test_length_is_bounded(self):
        assert len(_safe_segment("a" * 500)) <= 120

    def test_ordinary_names_survive_recognisably(self):
        assert _safe_segment("Lecture-3_notes.pdf") == "Lecture-3_notes.pdf"

    def test_document_path_is_scoped_to_course_and_document(self, storage):
        recorder, _ = storage()
        path = upload_course_document("maths-101", "doc-1", "notes.pdf", b"x", "application/pdf")
        assert path == "maths-101/doc-1/notes.pdf"
        assert recorder[0]["path"] == path


class TestCourseDocumentRetention:
    def test_uses_the_private_documents_bucket(self, storage):
        _, fake = storage()
        upload_course_document("c", "d", "f.pdf", b"x", "application/pdf")
        assert fake.storage.buckets == [COURSE_DOCS_BUCKET]

    def test_content_type_is_passed_through(self, storage):
        recorder, _ = storage()
        upload_course_document("c", "d", "f.pdf", b"x", "application/pdf")
        assert recorder[0]["options"]["content-type"] == "application/pdf"

    def test_reupload_overwrites_rather_than_duplicating(self, storage):
        """Re-ingesting the same document must not leave orphaned copies."""
        recorder, _ = storage()
        upload_course_document("c", "d", "f.pdf", b"x", "application/pdf")
        assert recorder[0]["options"]["upsert"] == "true"

    def test_failure_returns_none_instead_of_raising(self, storage):
        """Vectors are already live by this point; raising would mark a
        working document failed."""
        storage(fail=True)
        assert upload_course_document("c", "d", "f.pdf", b"x", "application/pdf") is None


class TestStudentImageRetention:
    def test_uses_the_private_uploads_bucket(self, storage):
        _, fake = storage()
        upload_student_image("u1", "s1", b"x", "image/png")
        assert fake.storage.buckets == [STUDENT_UPLOADS_BUCKET]

    def test_path_is_scoped_to_user_and_session(self, storage):
        recorder, _ = storage()
        upload_student_image("u1", "s1", b"x", "image/jpeg")
        assert recorder[0]["path"].startswith("u1/s1/")

    def test_extension_follows_the_media_type(self, storage):
        recorder, _ = storage()
        upload_student_image("u1", "s1", b"x", "image/webp")
        assert recorder[0]["path"].endswith(".webp")

    def test_two_uploads_never_collide(self, storage):
        recorder, _ = storage()
        upload_student_image("u1", "s1", b"x", "image/png")
        upload_student_image("u1", "s1", b"x", "image/png")
        assert recorder[0]["path"] != recorder[1]["path"]

    def test_does_not_overwrite(self, storage):
        """Unlike documents, each photo is a distinct submission."""
        recorder, _ = storage()
        upload_student_image("u1", "s1", b"x", "image/png")
        assert recorder[0]["options"]["upsert"] == "false"

    def test_failure_returns_none(self, storage):
        storage(fail=True)
        assert upload_student_image("u1", "s1", b"x", "image/png") is None


class TestSignedUrls:
    def test_mints_a_url(self, storage):
        storage()
        assert signed_url(STUDENT_UPLOADS_BUCKET, "u1/s1/a.png").startswith("https://sig/")

    def test_url_carries_an_expiry(self, storage):
        storage()
        assert "exp=" in signed_url(STUDENT_UPLOADS_BUCKET, "a.png")

    def test_empty_path_yields_nothing(self, storage):
        storage()
        assert signed_url(STUDENT_UPLOADS_BUCKET, "") is None

    def test_failure_yields_none_rather_than_a_broken_link(self, storage):
        storage(fail=True)
        assert signed_url(STUDENT_UPLOADS_BUCKET, "a.png") is None
