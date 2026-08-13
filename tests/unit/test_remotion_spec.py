"""The Remotion spec schema.

The whole security argument for this renderer rests on the schema: the model
produces data, fixed React components render it, and no generated code is ever
executed. So the tests are about whether the schema actually holds that line —
and whether it catches the malformed specs that would otherwise render a
plausible-looking but wrong video.
"""

import pytest
from pydantic import ValidationError

from app.media.render.remotion_spec import (
    MAX_STEPS,
    RemotionSpec,
    validate_spec,
)


def chart(labels=("a", "b", "c"), values=(1, 2, 3), name="series"):
    return {
        "kind": "bar",
        "x_labels": list(labels),
        "series": [{"name": name, "values": list(values)}],
    }


class TestArchetypeRequirements:
    def test_data_story_requires_a_chart(self):
        """A data story with no data renders a title card and stops."""
        with pytest.raises(ValidationError, match="requires a chart"):
            validate_spec({"archetype": "data_story", "title": "T"})

    @pytest.mark.parametrize("archetype", ["process_flow", "timeline"])
    def test_flows_require_steps(self, archetype):
        with pytest.raises(ValidationError, match="requires steps"):
            validate_spec({"archetype": archetype, "title": "T"})

    @pytest.mark.parametrize(
        "archetype", ["composited_explainer", "ui_or_code_walkthrough"]
    )
    def test_explainers_require_slides(self, archetype):
        with pytest.raises(ValidationError, match="requires slides"):
            validate_spec({"archetype": archetype, "title": "T"})

    def test_a_complete_data_story_validates(self):
        spec = validate_spec({"archetype": "data_story", "title": "T", "chart": chart()})
        assert spec.chart is not None

    def test_an_unknown_archetype_is_refused(self):
        with pytest.raises(ValidationError):
            validate_spec({"archetype": "interpretive_dance", "title": "T"})


class TestChartIntegrity:
    def test_series_must_match_the_axis(self):
        """A short series renders a truncated chart that looks plausible and is
        wrong — the failure a lecturer would not spot."""
        with pytest.raises(ValidationError, match="but there are 3 x labels"):
            validate_spec({
                "archetype": "data_story", "title": "T",
                "chart": chart(labels=("a", "b", "c"), values=(1, 2)),
            })

    def test_a_longer_series_is_also_refused(self):
        with pytest.raises(ValidationError):
            validate_spec({
                "archetype": "data_story", "title": "T",
                "chart": chart(labels=("a", "b"), values=(1, 2, 3)),
            })

    def test_a_single_point_is_not_a_chart(self):
        with pytest.raises(ValidationError):
            validate_spec({
                "archetype": "data_story", "title": "T",
                "chart": chart(labels=("a",), values=(1,)),
            })

    def test_chart_kinds_are_a_closed_set(self):
        with pytest.raises(ValidationError):
            validate_spec({
                "archetype": "data_story", "title": "T",
                "chart": {**chart(), "kind": "pie"},
            })


class TestBounds:
    def test_too_many_steps_is_refused(self):
        with pytest.raises(ValidationError):
            validate_spec({
                "archetype": "process_flow", "title": "T",
                "steps": [{"label": f"s{i}"} for i in range(MAX_STEPS + 1)],
            })

    def test_slide_duration_is_bounded(self):
        """A lecturer cannot fix a 40-second pause without a re-render."""
        with pytest.raises(ValidationError):
            validate_spec({
                "archetype": "composited_explainer", "title": "T",
                "slides": [{"title": "s", "seconds": 40}],
            })

    def test_overlong_text_is_refused(self):
        with pytest.raises(ValidationError):
            validate_spec({
                "archetype": "composited_explainer", "title": "T",
                "slides": [{"title": "x" * 500, "seconds": 4}],
            })

    def test_accent_must_be_a_hex_colour(self):
        """It goes straight into a style attribute."""
        with pytest.raises(ValidationError):
            validate_spec({
                "archetype": "process_flow", "title": "T",
                "steps": [{"label": "a"}], "accent": "red; content: url(x)",
            })

    def test_a_valid_accent_passes(self):
        spec = validate_spec({
            "archetype": "process_flow", "title": "T",
            "steps": [{"label": "a"}], "accent": "#ff8800",
        })
        assert spec.accent == "#ff8800"


class TestUnknownFieldsCannotReachTheRenderer:
    def test_extra_keys_are_dropped(self):
        """Anything the schema does not name cannot reach React."""
        spec = validate_spec({
            "archetype": "process_flow", "title": "T",
            "steps": [{"label": "a"}],
            "onLoad": "alert(1)", "dangerouslySetInnerHTML": "<script>",
        })
        dumped = spec.model_dump()
        assert "onLoad" not in dumped
        assert "dangerouslySetInnerHTML" not in dumped

    def test_the_dump_contains_only_schema_fields(self):
        spec = validate_spec({
            "archetype": "process_flow", "title": "T", "steps": [{"label": "a"}],
        })
        assert set(spec.model_dump()) == {
            "archetype", "title", "subtitle", "slides", "steps", "chart", "accent",
        }


class TestDuration:
    def test_a_minimal_video_still_has_a_title_and_an_outro(self):
        spec = validate_spec({
            "archetype": "process_flow", "title": "T", "steps": [{"label": "a"}],
        })
        assert spec.duration_seconds == pytest.approx(2.5 + 3.0 + 1.5)

    def test_slides_contribute_their_own_time(self):
        spec = validate_spec({
            "archetype": "composited_explainer", "title": "T",
            "slides": [{"title": "a", "seconds": 5}, {"title": "b", "seconds": 3}],
        })
        assert spec.duration_seconds == pytest.approx(2.5 + 8 + 1.5)

    def test_a_full_video_stays_under_three_minutes(self):
        """Proposal §6.2 caps concept videos at three minutes; the bounds have
        to make that true by construction, not by asking nicely."""
        spec = RemotionSpec(
            archetype="composited_explainer",
            title="T",
            slides=[{"title": f"s{i}", "seconds": 15} for i in range(MAX_STEPS)],
            steps=[{"label": f"t{i}"} for i in range(MAX_STEPS)],
            chart=chart(),
        )
        assert spec.duration_seconds <= 180
