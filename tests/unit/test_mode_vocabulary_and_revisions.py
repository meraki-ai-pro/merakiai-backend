"""The three modes, and re-rendering a video from an edited prompt.

**Vocabulary.** The product now says Learn / Review / Assessment everywhere.
The wire value for the third is still ``application`` — renaming a column and a
mode string across sessions, mode_sessions, document_chunks and every stored
conversation would be a data migration with no user-visible benefit — so the
rule is: ``application`` on the wire, "Assessment" in anything a person reads.

**Scenario topics.** The picker in front of Assessment mode is gone. Its five
options were generic labels a student had no basis to choose between, and
picking one narrowed the retrieval seed to a slice of the course for no
pedagogical reason.
"""

import inspect
from pathlib import Path

import pytest

from app.ai.rag.modes_sessions import service as modes
from app.ai.rag.modes_sessions.router import REVIEW_QUESTION_FORMATS
from app.models.models import ModeSessionStartRequest

BACKEND = Path(__file__).resolve().parents[2]


class TestAssessmentModeNeedsNoTopic:
    def test_session_type_is_optional_on_the_wire(self):
        payload = ModeSessionStartRequest(session_id="s1", mode="application")
        assert payload.session_type is None

    def test_review_still_accepts_a_question_format(self):
        payload = ModeSessionStartRequest(
            session_id="s1", mode="review", session_type="mcq"
        )
        assert payload.session_type == "mcq"

    def test_the_placeholder_is_a_real_value_not_an_empty_string(self):
        """mode_sessions.session_type is NOT NULL, and logs and existing rows
        all assume something is there."""
        assert modes.GENERAL_SCENARIO
        assert isinstance(modes.GENERAL_SCENARIO, str)

    def test_flashcard_is_gone_from_the_student_picker(self):
        """Removed at the client's request. It stays accepted on the wire
        because sessions started before the change still carry it — refusing
        it would break a half-finished set mid-turn."""
        constants = (
            BACKEND.parent / "merakiai-frontend/src/lib/constants.ts"
        )
        if not constants.exists():  # pragma: no cover — backend-only checkout
            pytest.skip("frontend not present")
        source = constants.read_text(encoding="utf-8")
        review_block = source[source.index("REVIEW_SESSION_TYPES"):]
        review_block = review_block[: review_block.index("] as const")]
        assert "flashcard" not in review_block

    def test_a_review_session_without_a_format_is_refused(self):
        """Defaulting silently would hand a student MCQs when they asked for
        short answer — the generator branches on this value."""
        from app.ai.rag.modes_sessions import router

        source = inspect.getsource(router.start_mode_session)
        assert "REVIEW_QUESTION_FORMATS" in source
        assert "mcq" in REVIEW_QUESTION_FORMATS
        assert "short_answer" in REVIEW_QUESTION_FORMATS

    def test_the_neutral_type_does_not_reach_the_prompt(self):
        """A placeholder in the seed steers retrieval at a word that means
        nothing — which is what "flotation_basics" did to every non-chemistry
        course before the topics were genericised."""
        source = inspect.getsource(modes.generate_application_scenario)
        assert "GENERAL_SCENARIO" in source
        assert "topical" in source


class TestSuperAdminIsAnAdmin:
    """A super_admin must never be locked out of the admin console.

    `role === 'admin'` in the frontend guards did exactly that: the console
    bounced super admins to /dashboard, which made the super-admin-only role
    management unreachable by the only role allowed to use it. The backend has
    always had ADMIN_ROLES; the frontend now has isAdminRole, and this stops
    the equality comparison creeping back.
    """

    FRONTEND = BACKEND.parent / "merakiai-frontend"
    GUARDS = [
        "src/components/admin/layout/AdminAuthGuard.tsx",
        "src/components/dashboard/AuthGuard.tsx",
        "src/store/userStore.ts",
    ]

    def test_backend_admin_roles_include_super_admin(self):
        from app.core.auth import ADMIN_ROLES

        assert "super_admin" in ADMIN_ROLES and "admin" in ADMIN_ROLES

    @pytest.mark.parametrize("relative", GUARDS)
    def test_no_equality_comparison_against_admin(self, relative):
        path = self.FRONTEND / relative
        if not path.exists():  # pragma: no cover — backend-only checkout
            pytest.skip("frontend not present")

        source = path.read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in source.splitlines()
            # Comments explain the rule; only real comparisons matter.
            if not line.strip().startswith(("//", "*", "/*"))
            and ("=== 'admin'" in line or '=== "admin"' in line
                 or "!== 'admin'" in line or '!== "admin"' in line)
        ]
        assert not offenders, (
            f"{relative} compares a role against 'admin' directly: {offenders}. "
            "Use isAdminRole() — a super_admin is an admin and then some."
        )

    @pytest.mark.parametrize("relative", GUARDS)
    def test_guards_use_the_shared_helper(self, relative):
        path = self.FRONTEND / relative
        if not path.exists():  # pragma: no cover
            pytest.skip("frontend not present")
        assert "isAdminRole" in path.read_text(encoding="utf-8")


class TestStudentFacingVocabulary:
    def test_scenario_prompt_focuses_on_context_and_question(self):
        scenario = {
            "type": modes.GENERAL_SCENARIO,
            "scenario_id": "S-1",
            "title": "Costing a batch",
            "difficulty": "Basic",
            "situation": "A factory...",
            "available_data": ["unit cost 4.20"],
            "guided_questions": ["What is the total?", "b", "c"],
        }
        prompt = modes.format_application_prompt(scenario, 1)
        assert "A factory..." in prompt
        assert "unit cost 4.20" in prompt
        assert "What is the total?" in prompt
        assert "Scenario ID" not in prompt
        assert "Costing a batch" not in prompt
        assert "Difficulty" not in prompt

    def test_internal_scenario_type_is_not_shown(self):
        scenario = {
            "type": "problem_solving",
            "situation": "A plant has stopped.",
            "available_data": [],
            "guided_questions": ["q", "b", "c"],
        }
        assert "problem_solving" not in modes.format_application_prompt(scenario, 1)

    def test_review_prompt_hides_generation_metadata(self):
        item = {
            "type": "mcq",
            "question_id": "REV-MCQ-0001",
            "difficulty": "Basic",
            "category": "Derivatives",
            "question": "What is the derivative of x squared?",
            "options": {"A": "x", "B": "2x", "C": "2", "D": "x squared"},
        }
        prompt = modes.format_review_prompt(item)
        assert "What is the derivative" in prompt
        assert "REV-MCQ-0001" not in prompt
        assert "Difficulty" not in prompt
        assert "Category" not in prompt

    def test_the_wire_value_is_unchanged(self):
        """The rename is presentation only. Changing this string would orphan
        every stored session, conversation and document chunk."""
        payload = ModeSessionStartRequest(session_id="s", mode="application")
        assert payload.mode == "application"


class TestVideoRevisions:
    def test_regenerate_links_to_the_asset_it_replaces(self):
        from app.api.v1 import render

        source = inspect.getsource(render.regenerate_asset)
        assert "parent_asset_id=asset_id" in source
        assert "revision=" in source

    def test_regenerate_refuses_an_unchanged_prompt(self):
        """It would return the cached render and look like nothing happened."""
        from app.api.v1 import render

        source = inspect.getsource(render.regenerate_asset)
        assert "unchanged" in source.lower()

    def test_regenerate_inherits_rather_than_resets_the_archetype(self):
        """A form that does not resend the archetype must not silently reroute
        a Chemistry video to Manim."""
        from app.api.v1 import render

        source = inspect.getsource(render.regenerate_asset)
        assert 'payload.archetype or original.get("archetype")' in source

    def test_approving_a_revision_retires_the_one_it_replaces(self):
        """Otherwise a concept accumulates approved renders and the lecturer
        sees three "Live" videos with no way to tell which students get."""
        from app.api.v1 import render

        source = inspect.getsource(render.review_asset)
        assert "Superseded" in source
        assert ".neq(" in source

    def test_playback_picks_the_newest_approval(self):
        from app.media.render import service

        source = inspect.getsource(service.playable_asset)
        assert '.order("approved_at", desc=True)' in source

    def test_the_old_video_keeps_serving_until_the_new_one_is_approved(self):
        """A regenerate creates a NEW asset. Overwriting in place would leave
        the course with no video at all for the minutes the re-render takes."""
        from app.api.v1 import render

        source = inspect.getsource(render.regenerate_asset)
        assert "request_render(" in source
        assert "update(" not in source


class TestSubjectRouting:
    @pytest.mark.parametrize(
        "subject,expected",
        [
            ("BSc Biology", "remotion"),
            ("Organic Chemistry II", "remotion"),
            ("Human Anatomy", "remotion"),
            ("Financial Accounting", "remotion"),
            ("Quantitative Techniques II", "manim"),
            ("Further Mathematics", "manim"),
            ("Engineering Mechanics", "manim"),
        ],
    )
    def test_free_text_course_names_route_sensibly(self, subject, expected):
        from app.media.render.routing import route

        assert route(None, subject) == expected

    def test_the_more_specific_subject_wins_on_a_compound_name(self):
        """"Mathematical Biology" contains both a maths word and "biology";
        the one that describes the course is the specific one."""
        from app.media.render.routing import route

        assert route(None, "Mathematical Biology") == "remotion"

    def test_the_full_ghanaian_course_list_routes_correctly(self):
        """Runs scripts/check_concept_videos.py's table in CI.

        That script is what a person runs before a demo; this is what stops the
        table rotting between demos. It caught "Pharmacology" falling through
        to the default renderer, because the substring match is not a stemmer
        and "pharmacy" is not a substring of "pharmacology".
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "check_concept_videos", BACKEND / "scripts" / "check_concept_videos.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        from app.media.render.routing import route

        wrong = [
            (name, expected, route(None, name))
            for expected, names in module.COURSE_NAMES.items()
            for name in names
            if route(None, name) != expected
        ]
        assert not wrong, f"Misrouted course names: {wrong}"

    def test_the_api_reads_the_subject_from_the_course(self):
        """It used to arrive from the client as a hard-coded "mathematics" on
        every request, which routed every untyped Biology and Chemistry video
        to an animation engine for continuous mathematics."""
        from app.api.v1 import render

        source = inspect.getsource(render.create_render)
        assert "_course_subject(" in source
