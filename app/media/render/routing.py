"""Choosing a renderer.

Routing is by **archetype** — what the visual actually does — not by subject.

Subject is an appealing rule and a wrong one. "Chemistry uses Remotion" fails
because reaction mechanisms and molecular structure are neither Remotion's nor
Manim's strength; that is RDKit / 3Dmol territory, and Remotion would produce a
well-animated slideshow *about* chemistry rather than chemistry visualised.
"Computer science uses Manim" fails in the other direction: animating a
red-black tree rebalancing is easier and clearer in Remotion, because it is
React and you can render the real DOM of the structure.

So archetype decides, and subject only supplies a default when the lesson
script does not say. A maths course still lands on Manim and a chemistry course
still lands on Remotion — but neither is locked in when the specific visual
wants the other tool.
"""

from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

Renderer = Literal["manim", "remotion"]

# What Manim is genuinely better at: continuous mathematics, constructed
# geometry, anything where the animation *is* the derivation.
MANIM_ARCHETYPES = frozenset({
    "equation_transform",     # algebra rearranging step by step
    "geometric_construction", # circle theorems, vectors, loci
    "plot_animation",         # curves, tangents, area under a curve
    "vector_field",           # gradients, flows, phase portraits
    "number_line",            # inequalities, intervals, limits
    "matrix_operation",       # row reduction, simplex tableaux
})

# What Remotion is genuinely better at: composed, designed motion over
# discrete content — including data structures, which are DOM-shaped.
REMOTION_ARCHETYPES = frozenset({
    "data_story",              # charts built from a dataset
    "process_flow",            # pipelines, decision trees, flowcharts
    "composited_explainer",    # images, diagrams and captions over a timeline
    "ui_or_code_walkthrough",  # algorithms, data structures, code
    "timeline",                # historical or procedural sequences
})

# Archetypes neither tool serves well. Recorded rather than silently misrouted,
# because producing a confident bad animation of a reaction mechanism is worse
# than admitting the gap.
UNSUPPORTED_ARCHETYPES = frozenset({
    "molecular_structure",  # needs RDKit / 3Dmol
    "anatomical_model",     # needs a 3D asset pipeline
})

# Fallback when the lesson script names no archetype. This is where subject
# finally gets a say — as a default, not a rule.
SUBJECT_DEFAULTS: dict[str, Renderer] = {
    "mathematics": "manim",
    "maths": "manim",
    "math": "manim",
    "calculus": "manim",
    "statistics": "manim",
    "quantitative techniques": "manim",
    "physics": "manim",
    "engineering": "manim",
    "computer science": "remotion",
    "chemistry": "remotion",
    "biology": "remotion",
    "economics": "remotion",
    "business": "remotion",
}

DEFAULT_RENDERER: Renderer = "manim"


class UnsupportedArchetypeError(ValueError):
    """The visual needs a tool this pipeline does not have."""


def route(archetype: str | None = None, subject: str | None = None) -> Renderer:
    """Pick a renderer for one visual.

    ``archetype`` wins when present. ``subject`` is consulted only as a
    fallback, and is matched loosely because it arrives from free-text course
    metadata ("BSc Mathematics", "Quantitative Techniques II").
    """
    key = (archetype or "").strip().lower().replace("-", "_").replace(" ", "_")

    if key in UNSUPPORTED_ARCHETYPES:
        raise UnsupportedArchetypeError(
            f"No renderer covers {key!r}. It needs a domain-specific tool "
            "(e.g. RDKit for molecular structure) rather than Manim or Remotion."
        )

    if key in MANIM_ARCHETYPES:
        return "manim"
    if key in REMOTION_ARCHETYPES:
        return "remotion"

    if key:
        logger.info("Unknown archetype %r — falling back to subject", archetype)

    subject_key = (subject or "").strip().lower()
    if subject_key:
        if subject_key in SUBJECT_DEFAULTS:
            return SUBJECT_DEFAULTS[subject_key]
        # Free-text course names rarely match exactly.
        for name, renderer in SUBJECT_DEFAULTS.items():
            if name in subject_key:
                return renderer

    return DEFAULT_RENDERER


def known_archetypes() -> list[str]:
    """Every archetype a lesson script may name, for prompts and validation."""
    return sorted(MANIM_ARCHETYPES | REMOTION_ARCHETYPES)
