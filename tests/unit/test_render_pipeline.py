"""Renderer routing, registry and job caching.

The routing tests encode the argument for routing by archetype rather than by
subject: the subject rule misroutes at both ends, and these cases are the
counter-examples.
"""

import pytest

from app.media.render import registry
from app.media.render.registry import RenderRequest, RenderResult
from app.media.render.routing import (
    DEFAULT_RENDERER,
    MANIM_ARCHETYPES,
    REMOTION_ARCHETYPES,
    UnsupportedArchetypeError,
    known_archetypes,
    route,
)
from app.media.render.service import content_hash


class TestArchetypeRouting:
    @pytest.mark.parametrize("archetype", sorted(MANIM_ARCHETYPES))
    def test_continuous_maths_goes_to_manim(self, archetype):
        assert route(archetype) == "manim"

    @pytest.mark.parametrize("archetype", sorted(REMOTION_ARCHETYPES))
    def test_composed_motion_goes_to_remotion(self, archetype):
        assert route(archetype) == "remotion"

    @pytest.mark.parametrize(
        "written", ["equation-transform", "Equation Transform", "  EQUATION_TRANSFORM  "]
    )
    def test_archetype_spelling_is_forgiving(self, written):
        """Lesson scripts are model-generated prose; exact casing is not a
        promise worth relying on."""
        assert route(written) == "manim"

    def test_archetype_beats_subject(self):
        """The whole point: a maths course wanting a flowchart gets Remotion."""
        assert route("process_flow", subject="Calculus") == "remotion"

    def test_a_cs_course_wanting_a_graph_still_gets_manim(self):
        assert route("plot_animation", subject="Computer Science") == "manim"


class TestSubjectFallback:
    @pytest.mark.parametrize(
        "subject", ["mathematics", "Calculus", "Statistics", "physics", "Engineering"]
    )
    def test_quantitative_subjects_default_to_manim(self, subject):
        assert route(None, subject) == "manim"

    @pytest.mark.parametrize("subject", ["chemistry", "Biology", "Economics"])
    def test_other_subjects_default_to_remotion(self, subject):
        assert route(None, subject) == "remotion"

    def test_free_text_course_names_still_match(self):
        """Course names arrive as 'BSc Mathematics', not 'mathematics'."""
        assert route(None, "BSc Mathematics Year 1") == "manim"
        assert route(None, "Introductory Chemistry (CHEM 101)") == "remotion"

    def test_computer_science_defaults_to_remotion(self):
        """Deliberately NOT manim: animating a data structure is easier in
        React, where you can render the real DOM of the structure."""
        assert route(None, "Computer Science") == "remotion"

    def test_unknown_subject_falls_back_to_the_default(self):
        assert route(None, "Basket Weaving") == DEFAULT_RENDERER

    def test_nothing_at_all_still_returns_a_renderer(self):
        assert route(None, None) == DEFAULT_RENDERER


class TestUnsupportedArchetypes:
    @pytest.mark.parametrize("archetype", ["molecular_structure", "anatomical_model"])
    def test_they_raise_rather_than_misroute(self, archetype):
        """Producing a confident bad animation of a reaction mechanism is
        worse than admitting the gap."""
        with pytest.raises(UnsupportedArchetypeError):
            route(archetype)

    def test_the_error_names_a_real_alternative(self):
        with pytest.raises(UnsupportedArchetypeError, match="RDKit"):
            route("molecular_structure")

    def test_they_are_not_offered_as_choices(self):
        assert "molecular_structure" not in known_archetypes()


class TestRegistry:
    def setup_method(self):
        registry.clear()

    def teardown_method(self):
        registry.clear()

    def test_register_then_get(self):
        class Fake:
            name = "fake"

            async def render(self, request):
                return RenderResult(b"", "video/mp4", "mp4")

        registry.register(Fake())
        assert registry.get("fake").name == "fake"

    def test_unknown_renderer_raises_a_useful_error(self):
        with pytest.raises(LookupError, match="No renderer registered"):
            registry.get("nope")

    def test_lazy_registration_defers_construction(self):
        """The API process must not import manim to know it exists."""
        built = {"n": 0}

        class Fake:
            name = "lazy"

            async def render(self, request):
                return RenderResult(b"", "video/mp4", "mp4")

        def factory():
            built["n"] += 1
            return Fake()

        registry.register_lazy("lazy", factory)
        assert built["n"] == 0
        assert registry.resolve("lazy").name == "lazy"
        assert built["n"] == 1
        registry.resolve("lazy")
        assert built["n"] == 1  # constructed once


class TestCacheKey:
    def test_identical_scripts_hash_alike(self):
        assert content_hash("teach the chain rule") == content_hash("teach the chain rule")

    def test_surrounding_whitespace_is_ignored(self):
        """A re-request that differs only by a trailing newline must be a cache
        hit, not another multi-minute render."""
        assert content_hash("  teach it  \n") == content_hash("teach it")

    def test_different_scripts_hash_differently(self):
        assert content_hash("chain rule") != content_hash("product rule")

    def test_empty_and_none_are_stable(self):
        assert content_hash("") == content_hash(None)


class TestRenderRequestShape:
    def test_carries_what_a_renderer_needs(self):
        req = RenderRequest(
            asset_id="a", course_id="c", concept_key="chain-rule",
            source_script="script", archetype="equation_transform",
        )
        assert req.concept_key == "chain-rule"
        assert req.duration_hint_seconds == 120

    def test_is_immutable(self):
        """A renderer must not be able to mutate the job it was handed."""
        req = RenderRequest(asset_id="a", course_id="c", concept_key="k", source_script="s")
        with pytest.raises(Exception):
            req.concept_key = "other"  # type: ignore[misc]
