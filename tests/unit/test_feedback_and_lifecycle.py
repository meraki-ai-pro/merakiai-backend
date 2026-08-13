"""Feedback validation and the deletion lifecycle.

The lifecycle tests are the ones that matter. Purge is the only irreversible
operation in the system, it deletes real students' work, and it runs
unattended — so the guard rails are the feature.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.v1.feedback_suite import NPS_COOLDOWN_DAYS, FeedbackIn, _nps
from app.core import lifecycle


class TestFeedbackValidation:
    def test_nps_requires_a_score(self):
        """A table of NPS rows with null scores is discovered at the end of a
        pilot, when it is too late to re-collect."""
        with pytest.raises(ValidationError):
            FeedbackIn(feedback_type="nps")

    def test_nps_rejects_the_wrong_scale(self):
        """NPS is 0-10; rating is 1-5. Accepting both invites someone to
        average them later."""
        with pytest.raises(ValidationError):
            FeedbackIn(feedback_type="nps", nps_score=9, rating=5)

    @pytest.mark.parametrize("kind", ["micro", "mode"])
    def test_rating_types_require_a_rating(self, kind):
        with pytest.raises(ValidationError):
            FeedbackIn(feedback_type=kind, mode="learn")

    def test_mode_feedback_requires_the_mode(self):
        with pytest.raises(ValidationError):
            FeedbackIn(feedback_type="mode", rating=4)

    def test_valid_submissions_pass(self):
        FeedbackIn(feedback_type="nps", nps_score=10)
        FeedbackIn(feedback_type="micro", rating=3)
        FeedbackIn(feedback_type="mode", rating=5, mode="learn")
        FeedbackIn(feedback_type="exit", free_text="left the course")
        FeedbackIn(feedback_type="lecturer", free_text="saves me time")

    @pytest.mark.parametrize("bad", [-1, 11])
    def test_nps_range_is_enforced(self, bad):
        with pytest.raises(ValidationError):
            FeedbackIn(feedback_type="nps", nps_score=bad)

    @pytest.mark.parametrize("bad", [0, 6])
    def test_rating_range_is_enforced(self, bad):
        with pytest.raises(ValidationError):
            FeedbackIn(feedback_type="micro", rating=bad)


class TestNpsMaths:
    def test_no_responses_is_not_zero(self):
        """Zero NPS means 'as many detractors as promoters', not 'no data'."""
        assert _nps([])["measured"] is False

    def test_all_promoters(self):
        assert _nps([9, 10, 10])["score"] == 100

    def test_all_detractors(self):
        assert _nps([0, 3, 6])["score"] == -100

    def test_passives_count_in_the_denominator(self):
        """Two promoters, two passives → 50, not 100. Dropping passives is the
        classic way to inflate the number."""
        result = _nps([9, 10, 7, 8])
        assert result["passives"] == 2
        assert result["score"] == 50

    def test_boundaries(self):
        r = _nps([6, 7, 8, 9])
        assert r["detractors"] == 1 and r["passives"] == 2 and r["promoters"] == 1

    def test_cooldown_is_about_three_weeks(self):
        assert 14 <= NPS_COOLDOWN_DAYS <= 28


class TestPurgeGuardRails:
    def test_purge_refuses_a_live_account(self, monkeypatch):
        """The guard that stops an irreversible delete running against the
        wrong id."""
        class FakeTable:
            def select(self, *_a):
                return self

            def eq(self, *_a):
                return self

            def execute(self):
                return type("R", (), {"data": [{"id": "u1", "deleted_at": None}]})()

        monkeypatch.setattr(
            lifecycle, "get_supabase",
            lambda: type("S", (), {"table": lambda s, n: FakeTable()})(),
        )
        with pytest.raises(ValueError, match="soft-deleted first"):
            lifecycle.purge("u1")

    def test_unknown_user_raises_lookup(self, monkeypatch):
        class Empty:
            def select(self, *_a):
                return self

            def eq(self, *_a):
                return self

            def execute(self):
                return type("R", (), {"data": []})()

        monkeypatch.setattr(
            lifecycle, "get_supabase",
            lambda: type("S", (), {"table": lambda s, n: Empty()})(),
        )
        with pytest.raises(LookupError):
            lifecycle.purge("ghost")

    def test_run_purge_defaults_to_a_dry_run(self):
        import inspect
        from app.api.v1.lifecycle import run_purge

        assert inspect.signature(run_purge).parameters["dry_run"].default is True

    def test_only_super_admin_may_purge(self):
        src = Path(lifecycle.__file__).parent.parent / "api/v1/lifecycle.py"
        assert "Only a super admin may run a purge" in src.read_text(encoding="utf-8")

    def test_deletion_requires_a_typed_phrase(self):
        """A mis-sent JSON body must not be able to delete an account."""
        src = (Path(lifecycle.__file__).parent.parent / "api/v1/lifecycle.py").read_text(
            encoding="utf-8"
        )
        assert 'DELETE MY ACCOUNT' in src


class TestRetentionAndConsent:
    def test_retention_window_is_meaningful_but_finite(self):
        """Long enough to undo a mistake, short enough to be a real promise."""
        assert 7 <= lifecycle.RETENTION_DAYS <= 90

    def test_email_hash_is_stable_and_case_insensitive(self):
        a = lifecycle._hash_email("Student@University.edu")
        b = lifecycle._hash_email("  student@university.edu  ")
        assert a == b and len(a) == 64

    def test_email_hash_of_nothing_is_nothing(self):
        assert lifecycle._hash_email(None) is None
        assert lifecycle._hash_email("") is None

    def test_consented_events_are_anonymised_not_deleted(self):
        """Roadmap Part D: keep anonymised aggregates. The count survives, the
        person does not."""
        src = Path(lifecycle.__file__).read_text(encoding="utf-8")
        assert "anonymise_user_events" in src
        assert "events_anonymised" in src

    def test_transcripts_go_regardless_of_consent(self):
        """Consent covers aggregates, not a student's own words and
        photographs of their work."""
        src = Path(lifecycle.__file__).read_text(encoding="utf-8")
        purge_body = src[src.index("def purge("):]
        for table in ("conversations", "sessions", "assessment_attempts"):
            assert table in purge_body

    def test_storage_is_cleared_before_the_rows_that_point_at_it(self):
        """The other order loses the pointers and leaves orphans nobody can
        find."""
        src = Path(lifecycle.__file__).read_text(encoding="utf-8")
        body = src[src.index("def purge("):]
        assert body.index("_purge_storage") < body.index('("conversations"')

    def test_restore_does_not_silently_re_enrol(self):
        """Withdrawal may have been a separate, deliberate lecturer decision."""
        src = Path(lifecycle.__file__).read_text(encoding="utf-8")
        assert "enrolments_restored" in src

    def test_soft_delete_withdraws_enrolments_immediately(self):
        """Access must stop on the next turn, not when the purge runs weeks
        later."""
        src = Path(lifecycle.__file__).read_text(encoding="utf-8")
        soft = src[src.index("def soft_delete("): src.index("def restore(")]
        assert "withdrawn" in soft
