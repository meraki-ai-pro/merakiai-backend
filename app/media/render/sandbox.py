"""Static validation of model-generated Manim scene code.

A Manim scene is Python, and this pipeline gets its Python from an LLM. That is
arbitrary code execution on our own infrastructure, reached by a path that
starts at a lecturer typing a topic name.

There is a direct precedent in this codebase: Lesson Board plot expressions are
compiled by a shunting-yard parser rather than ``eval`` precisely because they
are model-generated. This is the same threat with a far larger blast radius —
a full interpreter instead of an arithmetic string.

This module is the FIRST of two layers and the weaker one. An AST allowlist can
be defeated by something nobody thought of; container isolation cannot. Both
are required:

  1. this validator, which rejects the obvious and the known-clever, and
  2. a render container with no network, a read-only filesystem, a non-root
     user, and hard CPU / memory / wall-clock caps (see render.Dockerfile).

Never run scene code on the API host or a text worker.
"""

from __future__ import annotations

import ast
import logging

logger = logging.getLogger(__name__)


class UnsafeSceneError(ValueError):
    """Raised when generated code does something a diagram never needs to."""


# Modules a mathematical animation legitimately needs. Everything else is
# refused rather than reasoned about — an allowlist is the only form of this
# check that stays correct as the standard library grows.
ALLOWED_IMPORTS = frozenset({
    "manim",
    "math",
    "numpy",
    "np",
    "random",
    "itertools",
    "fractions",
    "decimal",
})

# Names that reach outside the process, mutate the interpreter, or re-enter the
# compiler. None of these has a use in drawing a graph.
FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "breakpoint", "memoryview", "help", "exit", "quit",
})

# Attribute access that leads out of the sandbox by walking the object graph:
# ``().__class__.__bases__[0].__subclasses__()`` is the classic route to
# arbitrary imports without ever writing the word "import".
FORBIDDEN_ATTRIBUTES = frozenset({
    "__class__", "__bases__", "__subclasses__", "__mro__", "__globals__",
    "__code__", "__closure__", "__func__", "__self__", "__dict__",
    "__builtins__", "__loader__", "__spec__", "__reduce__", "__reduce_ex__",
    "__getattribute__", "__base__", "__init_subclass__", "__subclasshook__",
})

# Hard ceiling. Legitimate scenes are well under this; a much larger file is
# either an attempt to bury something or a model that has lost the plot.
MAX_SOURCE_CHARS = 20_000
MAX_AST_NODES = 4_000


def _describe(node: ast.AST) -> str:
    line = getattr(node, "lineno", "?")
    return f"line {line}"


class _Auditor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scene_classes: list[str] = []

    # ── imports ──────────────────────────────────────────────────────────
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise UnsafeSceneError(
                    f"Import of {alias.name!r} is not allowed ({_describe(node)}). "
                    f"Permitted: {', '.join(sorted(ALLOWED_IMPORTS))}."
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # A relative import (``from . import x``) has module None and would
        # reach our own package tree.
        if node.level:
            raise UnsafeSceneError(f"Relative imports are not allowed ({_describe(node)}).")
        root = (node.module or "").split(".")[0]
        if root not in ALLOWED_IMPORTS:
            raise UnsafeSceneError(
                f"Import from {node.module!r} is not allowed ({_describe(node)})."
            )
        self.generic_visit(node)

    # ── names and attributes ─────────────────────────────────────────────
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES:
            raise UnsafeSceneError(f"Use of {node.id!r} is not allowed ({_describe(node)}).")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FORBIDDEN_ATTRIBUTES:
            raise UnsafeSceneError(
                f"Attribute {node.attr!r} is not allowed ({_describe(node)}) — "
                "introspection can be used to escape the allowed imports."
            )
        # Catch-all for dunders not enumerated above. Manim's own API uses no
        # dunder attributes, so this costs nothing and closes the gap left by
        # a fixed list.
        if node.attr.startswith("__") and node.attr.endswith("__"):
            raise UnsafeSceneError(
                f"Dunder attribute {node.attr!r} is not allowed ({_describe(node)})."
            )
        self.generic_visit(node)

    # ── statements with no place in a scene ──────────────────────────────
    def visit_Global(self, node: ast.Global) -> None:
        raise UnsafeSceneError(f"'global' is not allowed ({_describe(node)}).")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        raise UnsafeSceneError(f"'nonlocal' is not allowed ({_describe(node)}).")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
        bases |= {b.attr for b in node.bases if isinstance(b, ast.Attribute)}
        if bases & {"Scene", "MovingCameraScene", "ThreeDScene", "ZoomedScene"}:
            self.scene_classes.append(node.name)
        self.generic_visit(node)


def validate_scene(source: str) -> str:
    """Check generated scene code and return the Scene subclass name.

    Raises :class:`UnsafeSceneError` if the code is rejected. Returning the
    class name is deliberate: the caller needs it to invoke manim, and taking
    it from the parsed AST rather than a regex means the name that gets
    rendered is the name that was validated.
    """
    if not source or not source.strip():
        raise UnsafeSceneError("Scene code is empty.")

    if len(source) > MAX_SOURCE_CHARS:
        raise UnsafeSceneError(
            f"Scene code is {len(source)} characters, over the {MAX_SOURCE_CHARS} limit."
        )

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise UnsafeSceneError(f"Scene code does not parse: {exc.msg} (line {exc.lineno}).") from exc

    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > MAX_AST_NODES:
        raise UnsafeSceneError(f"Scene code is too complex ({node_count} nodes).")

    auditor = _Auditor()
    auditor.visit(tree)

    if not auditor.scene_classes:
        raise UnsafeSceneError(
            "No Scene subclass found. The code must define exactly one class "
            "deriving from Scene."
        )
    if len(auditor.scene_classes) > 1:
        raise UnsafeSceneError(
            f"Expected one Scene subclass, found {len(auditor.scene_classes)}: "
            f"{', '.join(auditor.scene_classes)}."
        )

    return auditor.scene_classes[0]
