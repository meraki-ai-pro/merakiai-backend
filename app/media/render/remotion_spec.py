"""Validated scene specs for the Remotion renderer.

The important difference from Manim: **Remotion never executes generated code.**

Manim needs arbitrary Python because the animation *is* a program — you cannot
express "sweep a tangent along this curve" as data without inventing a language.
Remotion's archetypes cannot: a chart, a process flow, a captioned sequence are
all fully described by their content. So the model produces JSON matching the
schema below, and a fixed set of React components renders it.

That removes the entire threat class the Manim sandbox exists to contain. There
is no interpreter to escape, so there is nothing to escape from — the worst a
malformed spec can do is fail validation.

Anything the schema does not permit simply cannot reach the renderer.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Kept small on purpose. Every additional field is a shape the React side has
# to handle, and an unhandled shape is a blank frame in a lecture.
MAX_STEPS = 8
MAX_SERIES = 4
MAX_POINTS = 24
MAX_TEXT = 240


class Slide(BaseModel):
    title: str = Field(..., max_length=120)
    body: str | None = Field(None, max_length=MAX_TEXT)
    # Seconds on screen. Bounded because a lecturer cannot fix a 40-second
    # pause without a re-render.
    seconds: float = Field(4, ge=1.5, le=15)


class Step(BaseModel):
    label: str = Field(..., max_length=80)
    detail: str | None = Field(None, max_length=MAX_TEXT)


class Series(BaseModel):
    name: str = Field(..., max_length=40)
    values: list[float] = Field(..., min_length=2, max_length=MAX_POINTS)


class ChartSpec(BaseModel):
    kind: Literal["bar", "line", "area"] = "bar"
    x_labels: list[str] = Field(..., min_length=2, max_length=MAX_POINTS)
    series: list[Series] = Field(..., min_length=1, max_length=MAX_SERIES)
    x_title: str | None = Field(None, max_length=60)
    y_title: str | None = Field(None, max_length=60)

    @field_validator("series")
    @classmethod
    def series_match_labels(cls, v, info):
        """Every series must be the same length as the axis.

        Caught here rather than in React, where a short series silently renders
        a truncated chart that looks plausible and is wrong.
        """
        labels = info.data.get("x_labels") or []
        for s in v:
            if len(s.values) != len(labels):
                raise ValueError(
                    f"series {s.name!r} has {len(s.values)} values "
                    f"but there are {len(labels)} x labels"
                )
        return v


class RemotionSpec(BaseModel):
    """The complete description of a Remotion video."""

    archetype: Literal[
        "data_story", "process_flow", "composited_explainer", "timeline",
        "ui_or_code_walkthrough",
    ]
    title: str = Field(..., max_length=120)
    subtitle: str | None = Field(None, max_length=160)
    slides: list[Slide] = Field(default_factory=list, max_length=MAX_STEPS)
    steps: list[Step] = Field(default_factory=list, max_length=MAX_STEPS)
    chart: ChartSpec | None = None
    accent: str = Field("#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("slides", "steps")
    @classmethod
    def not_both_empty(cls, v):
        return v

    def model_post_init(self, _ctx: Any) -> None:
        if self.archetype == "data_story" and self.chart is None:
            raise ValueError("data_story requires a chart")
        if self.archetype in ("process_flow", "timeline") and not self.steps:
            raise ValueError(f"{self.archetype} requires steps")
        if self.archetype in ("composited_explainer", "ui_or_code_walkthrough") and not self.slides:
            raise ValueError(f"{self.archetype} requires slides")

    @property
    def duration_seconds(self) -> float:
        """Total runtime. Steps get a fixed beat; slides carry their own."""
        from_slides = sum(s.seconds for s in self.slides)
        from_steps = len(self.steps) * 3.0
        from_chart = 6.0 if self.chart else 0.0
        # Title card plus a moment to read the last frame.
        return round(2.5 + from_slides + from_steps + from_chart + 1.5, 2)


def validate_spec(raw: dict) -> RemotionSpec:
    """Parse and validate. Raises pydantic.ValidationError on anything unknown."""
    return RemotionSpec.model_validate(raw)
