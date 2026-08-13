"""Approved videos reaching the lesson board.

The property that matters: the model is handed a closed list of concept keys
that actually have an approved render. It never emits a URL, and it cannot
invent a key that resolves to something — a hallucinated key shows a student a
promise of a video and then nothing.
"""

import pytest

from app.ai.rag.prompt_builder import build_system_and_user


def build(**kwargs):
    defaults = dict(
        user_message="explain the chain rule",
        context=["some course text"],
        mode="learn",
        board=True,
    )
    defaults.update(kwargs)
    return build_system_and_user(**defaults)


class TestVideoDirective:
    def test_absent_when_the_course_has_no_videos(self):
        _, user = build(video_concepts=[])
        assert "::: video" not in user

    def test_absent_when_not_passed_at_all(self):
        _, user = build()
        assert "::: video" not in user

    def test_present_when_the_course_has_videos(self):
        _, user = build(video_concepts=["chain-rule"])
        assert "::: video" in user

    def test_every_available_key_is_listed(self):
        _, user = build(video_concepts=["chain-rule", "integration-by-parts"])
        assert "chain-rule" in user
        assert "integration-by-parts" in user

    def test_the_model_is_told_not_to_invent_keys(self):
        _, user = build(video_concepts=["chain-rule"])
        assert "ONLY a key from the list" in user

    def test_at_most_one_video_is_requested(self):
        _, user = build(video_concepts=["chain-rule"])
        assert "At most one video" in user


class TestDirectiveScoping:
    def test_no_video_directive_without_the_board(self):
        """A plain text or video answer has nowhere to put a video slide."""
        _, user = build(board=False, video_concepts=["chain-rule"])
        assert "::: video" not in user

    def test_the_video_block_rides_the_dynamic_user_text(self):
        """It must NOT land in the cached system prefix — the available videos
        change as a lecturer approves them, and a cached prefix would go stale."""
        system, user = build(video_concepts=["chain-rule"])
        assert "chain-rule" in user
        assert "chain-rule" not in system

    def test_the_board_directive_still_comes_first(self):
        _, user = build(video_concepts=["chain-rule"])
        assert user.index("::: slide") < user.index("::: video")


class TestApprovedKeyLookup:
    def test_only_approved_and_ready_assets_count(self, monkeypatch):
        from app.media.render import service

        captured = {}

        class FakeQuery:
            def select(self, *_a, **_k):
                return self

            def eq(self, col, value):
                captured[col] = value
                return self

            @property
            def not_(self):
                return self

            def is_(self, col, value):
                captured[f"not_{col}"] = value
                return self

            def execute(self):
                return type("R", (), {"data": [{"concept_key": "chain-rule"}]})()

        monkeypatch.setattr(
            service, "get_supabase", lambda: type("S", (), {"table": lambda s, n: FakeQuery()})()
        )

        assert service.approved_concept_keys("maths-101") == ["chain-rule"]
        assert captured["status"] == "ready"
        assert captured["not_approved_at"] == "null"
        assert captured["course_id"] == "maths-101"

    def test_lookup_failure_degrades_to_no_videos(self, monkeypatch):
        """A missing media_assets table must not fail the student's turn."""
        from app.media.render import service

        def boom():
            raise RuntimeError('relation "media_assets" does not exist')

        monkeypatch.setattr(service, "get_supabase", boom)
        assert service.approved_concept_keys("maths-101") == []

    def test_duplicate_keys_are_collapsed(self, monkeypatch):
        from app.media.render import service

        class FakeQuery:
            def select(self, *_a, **_k):
                return self

            def eq(self, *_a):
                return self

            @property
            def not_(self):
                return self

            def is_(self, *_a):
                return self

            def execute(self):
                return type("R", (), {"data": [
                    {"concept_key": "chain-rule"},
                    {"concept_key": "chain-rule"},
                    {"concept_key": "limits"},
                ]})()

        monkeypatch.setattr(
            service, "get_supabase", lambda: type("S", (), {"table": lambda s, n: FakeQuery()})()
        )
        assert service.approved_concept_keys("c") == ["chain-rule", "limits"]
