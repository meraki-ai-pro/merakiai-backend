"""Step 6 of the permission stack: published + mode-tagged documents only.

Ref: Meraki_AI_Student_Permission_Checks §3.4
     Meraki_AI_Lecturer_Side_Technical_Documentation §3.5

The distinction that carries the whole design: None means "no filter needed"
and an empty list means "nothing is visible". Conflating them either leaks
every draft or blanks every course.

_fetch() returns document ROWS, not ids: the per-student preference (question
format, difficulty) is applied on top of the cached set and varies turn by
turn, so the tags have to survive as far as prefer(). ids() below is what the
mode/publish assertions actually care about.
"""

import pytest

from app.ai.rag import visibility as vis
from app.ai.rag.retriever import _build_filter
from app.ai.ingestion.service import _VALID_MODES, parse_target_modes


class FakeTable:
    def __init__(self, rows, raises):
        self.rows = rows
        self.raises = raises
        self.course = None

    def select(self, *_a, **_k):
        return self

    def eq(self, _col, value):
        self.course = value
        return self

    def execute(self):
        if self.raises:
            raise self.raises
        return type("R", (), {"data": [r for r in self.rows if r.get("course_id") == self.course]})()


class FakeSupabase:
    def __init__(self, rows, raises=None):
        self.rows = rows
        self.raises = raises

    def table(self, _name):
        return FakeTable(self.rows, self.raises)


@pytest.fixture(autouse=True)
def clear_cache():
    vis.invalidate()
    yield
    vis.invalidate()


@pytest.fixture
def docs(monkeypatch):
    def _apply(rows, raises=None):
        monkeypatch.setattr(vis, "get_supabase", lambda: FakeSupabase(rows, raises))

    return _apply


def doc(
    did,
    published=True,
    modes=None,
    status="ready",
    deleted=None,
    course="maths-101",
    formats=None,
    difficulty=None,
):
    return {
        "id": did,
        "course_id": course,
        "is_published": published,
        "target_modes": modes,
        "status": status,
        "deleted_at": deleted,
        "question_formats": formats,
        "difficulty": difficulty,
    }


def ids(rows):
    """The document ids in a _fetch() result, preserving order."""
    return None if rows is None else [r["id"] for r in rows]


class TestNoFilterNeeded:
    def test_all_visible_returns_none(self, docs):
        """The common case must leave the Pinecone query untouched."""
        docs([doc("a"), doc("b")])
        assert vis._fetch("maths-101", "learn") is None

    def test_unknown_course_returns_none(self, docs):
        docs([])
        assert vis._fetch("nope", "learn") is None

    def test_missing_columns_fail_open(self, docs):
        """Before the migration lands, retrieval must keep working."""
        docs([], raises=Exception('column "is_published" does not exist'))
        assert vis._fetch("maths-101", "learn") is None


class TestFiltering:
    def test_draft_documents_are_excluded(self, docs):
        docs([doc("a"), doc("b", published=False)])
        assert ids(vis._fetch("maths-101", "learn")) == ["a"]

    def test_deleted_documents_are_excluded(self, docs):
        docs([doc("a"), doc("b", deleted="2026-08-01T00:00:00Z")])
        assert ids(vis._fetch("maths-101", "learn")) == ["a"]

    def test_unfinished_documents_are_excluded(self, docs):
        """A processing or failed document has partial vectors at best."""
        docs([doc("a"), doc("b", status="processing"), doc("c", status="failed")])
        assert ids(vis._fetch("maths-101", "learn")) == ["a"]

    def test_mode_tags_are_honoured(self, docs):
        """A past paper tagged review-only must not surface in a Learn answer —
        this is the leak §3.5 exists to prevent."""
        docs([doc("notes", modes=["learn"]), doc("pastpaper", modes=["review"])])
        assert ids(vis._fetch("maths-101", "learn")) == ["notes"]
        assert ids(vis._fetch("maths-101", "review")) == ["pastpaper"]

    def test_multi_mode_document_appears_in_both(self, docs):
        """One file of worked examples serving Learn and Review — the case the
        single default_mode column could not express."""
        docs([
            doc("worked", modes=["learn", "review"]),
            doc("other", modes=["review"]),
            doc("drill", modes=["application"]),
        ])
        assert ids(vis._fetch("maths-101", "learn")) == ["worked"]
        assert ids(vis._fetch("maths-101", "review")) == ["worked", "other"]
        assert ids(vis._fetch("maths-101", "application")) == ["drill"]

    def test_no_filter_when_every_document_serves_the_mode(self, docs):
        """All visible → None, so the Pinecone query stays exactly as before."""
        docs([doc("a", modes=["review"]), doc("b", modes=["review"])])
        assert vis._fetch("maths-101", "review") is None

    def test_untagged_document_serves_every_mode(self, docs):
        """target_modes NULL predates the feature and must not vanish."""
        docs([doc("legacy", modes=None), doc("draft", published=False)])
        assert ids(vis._fetch("maths-101", "application")) == ["legacy"]

    def test_everything_hidden_returns_empty_not_none(self, docs):
        """Empty list, not None — None would retrieve the drafts it excluded."""
        docs([doc("a", published=False), doc("b", published=False)])
        assert ids(vis._fetch("maths-101", "learn")) == []

    def test_other_courses_are_never_included(self, docs):
        docs([doc("a"), doc("x", published=False, course="chem-101")])
        assert vis._fetch("maths-101", "learn") is None


class TestPineconeFilter:
    def test_none_adds_no_id_clause(self):
        assert _build_filter("learn", None) == {"mode": {"$eq": "learn"}}

    def test_empty_list_is_honoured(self):
        """Must not be treated as 'no filter' — that would leak every draft."""
        assert _build_filter("learn", []) == {"mode": {"$eq": "learn"}, "document_id": {"$in": []}}

    def test_ids_are_passed_through(self):
        out = _build_filter("review", ["a", "b"])
        assert out["document_id"] == {"$in": ["a", "b"]}
        assert out["mode"] == {"$eq": "review"}


class TestCaching:
    @pytest.mark.asyncio
    async def test_repeat_lookups_hit_the_cache(self, docs, monkeypatch):
        calls = {"n": 0}
        real = vis._fetch

        def counted(c, m):
            calls["n"] += 1
            return real(c, m)

        docs([doc("a", published=False)])
        monkeypatch.setattr(vis, "_fetch", counted)
        await vis.visible_document_ids("maths-101", "learn")
        await vis.visible_document_ids("maths-101", "learn")
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_invalidate_forces_a_refetch(self, docs, monkeypatch):
        calls = {"n": 0}
        real = vis._fetch

        def counted(c, m):
            calls["n"] += 1
            return real(c, m)

        docs([doc("a", published=False)])
        monkeypatch.setattr(vis, "_fetch", counted)
        await vis.visible_document_ids("maths-101", "learn")
        vis.invalidate("maths-101")
        await vis.visible_document_ids("maths-101", "learn")
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_modes_are_cached_separately(self, docs, monkeypatch):
        docs([doc("notes", modes=["learn"])])
        assert await vis.visible_document_ids("maths-101", "learn") is None
        assert await vis.visible_document_ids("maths-101", "review") == []

    @pytest.mark.asyncio
    async def test_a_publish_in_another_process_is_not_missed(self, docs, monkeypatch):
        """The process that publishes is not the process that retrieves.

        Publishing is an API route; retrieval runs in the Celery text worker.
        `_cache` is per-process, so an in-process invalidate() clears the API's
        copy and leaves the worker answering from a stale set for the rest of
        the ten-minute TTL — the lecturer's own test-query panel shows the file
        gone while students are still taught from it.

        Here the local cache is deliberately NOT cleared: only the shared
        generation moves, which is exactly what the other process's publish
        looks like from this one.
        """
        generation = {"v": "1"}
        monkeypatch.setattr(vis, "_current_generation", lambda _c: generation["v"])

        calls = {"n": 0}
        real = vis._fetch

        def counted(c, m):
            calls["n"] += 1
            return real(c, m)

        docs([doc("a", published=False)])
        monkeypatch.setattr(vis, "_fetch", counted)

        await vis.visible_document_ids("maths-101", "learn")
        await vis.visible_document_ids("maths-101", "learn")
        assert calls["n"] == 1, "second lookup should have been served from cache"

        generation["v"] = "2"  # another process published
        await vis.visible_document_ids("maths-101", "learn")
        assert calls["n"] == 2, "a publish elsewhere must force a refetch here"

    @pytest.mark.asyncio
    async def test_redis_outage_degrades_to_the_ttl_rather_than_breaking(
        self, docs, monkeypatch
    ):
        """Caching must never take retrieval down with it.

        With no generation available every lookup carries the same None, so the
        cache still works and the TTL is the only backstop — the behaviour this
        had before the generation existed.
        """
        monkeypatch.setattr(vis, "_current_generation", lambda _c: None)

        calls = {"n": 0}
        real = vis._fetch

        def counted(c, m):
            calls["n"] += 1
            return real(c, m)

        docs([doc("a", published=False)])
        monkeypatch.setattr(vis, "_fetch", counted)

        assert await vis.visible_document_ids("maths-101", "learn") == []
        assert await vis.visible_document_ids("maths-101", "learn") == []
        assert calls["n"] == 1

    def test_invalidate_survives_an_unreachable_redis(self, monkeypatch):
        """A publish must not 500 because the generation could not be bumped."""
        def boom():
            raise RuntimeError("redis down")

        monkeypatch.setattr(vis, "_get_redis", boom)
        vis._cache[("maths-101", "learn")] = (0.0, "1", ["a"])

        vis.invalidate("maths-101")  # must not raise

        assert ("maths-101", "learn") not in vis._cache


class TestTargetModeParsing:
    def test_absent_means_default_only(self):
        assert parse_target_modes(None, "learn") is None
        assert parse_target_modes("   ", "learn") is None

    def test_parses_a_list(self):
        assert parse_target_modes("learn,review", "learn") == ["learn", "review"]

    def test_tolerates_whitespace_and_case(self):
        assert parse_target_modes(" Learn , REVIEW ", "learn") == ["learn", "review"]

    def test_deduplicates_preserving_order(self):
        assert parse_target_modes("review,learn,review", "learn") == ["review", "learn"]

    @pytest.mark.parametrize("bad", ["practice", "bogus", "learn,wizard"])
    def test_rejects_unknown_modes(self, bad):
        with pytest.raises(ValueError):
            parse_target_modes(bad, "learn")

    def test_valid_modes_match_the_app_vocabulary(self):
        assert set(_VALID_MODES) == {"learn", "review", "application"}
