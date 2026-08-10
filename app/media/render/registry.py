"""Renderer registry.

One protocol, N implementations. Adding Remotion later should be writing a
class and registering it, not touching the job service, the queue, the status
contract or the review gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class RenderRequest:
    """Everything a renderer needs, and nothing about how it is stored."""

    asset_id: str
    course_id: str
    concept_key: str
    source_script: str
    archetype: str | None = None
    topic: str | None = None
    duration_hint_seconds: int = 120


@dataclass(frozen=True)
class RenderResult:
    """What a renderer produces. ``content`` is the encoded artefact."""

    content: bytes
    media_type: str
    extension: str
    duration_seconds: float | None = None
    # The code that was executed, retained for lecturer review and for
    # reproducing a render without re-invoking the model.
    scene_code: str | None = None
    # Optional captions track. Proposal §6.2 requires every video to carry them.
    captions: str | None = None


class Renderer(Protocol):
    name: str

    async def render(self, request: RenderRequest) -> RenderResult:
        ...


_registry: dict[str, Renderer] = {}


def register(renderer: Renderer) -> Renderer:
    """Register a renderer. Usable as a decorator on the instance factory."""
    _registry[renderer.name] = renderer
    return renderer


def get(name: str) -> Renderer:
    try:
        return _registry[name]
    except KeyError:
        raise LookupError(
            f"No renderer registered under {name!r}. Available: "
            f"{', '.join(sorted(_registry)) or 'none'}"
        ) from None


def available() -> list[str]:
    return sorted(_registry)


def clear() -> None:
    """Test hook — never call this from application code."""
    _registry.clear()


# Deferred registration. Importing the Manim renderer at module import time
# would pull manim (and LaTeX bindings) into the API process, which does not
# have them and does not need them — only the render worker does.
_LAZY: dict[str, Callable[[], Renderer]] = {}


def register_lazy(name: str, factory: Callable[[], Renderer]) -> None:
    _LAZY[name] = factory


def resolve(name: str) -> Renderer:
    """Get a renderer, constructing it on first use if registered lazily."""
    if name not in _registry and name in _LAZY:
        _registry[name] = _LAZY[name]()
    return get(name)
