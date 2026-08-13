"""Static validation of model-generated Manim scenes.

These are adversarial tests. The code under validation is written by an LLM and
executed on our infrastructure, so the interesting cases are not "does a normal
scene pass" but "does a clever escape fail".

This is layer one of two. Container isolation is the layer that has to hold if
one of these is bypassed — see render.Dockerfile.
"""

import pytest

from app.media.render.sandbox import (
    ALLOWED_IMPORTS,
    MAX_SOURCE_CHARS,
    UnsafeSceneError,
    validate_scene,
)

GOOD = """
from manim import *
import numpy as np


class ChainRule(Scene):
    def construct(self):
        axes = Axes(x_range=[-3, 3], y_range=[-1, 9])
        graph = axes.plot(lambda x: x ** 2, color=BLUE)
        label = MathTex(r"\\frac{d}{dx}f(g(x)) = f'(g(x))g'(x)")
        self.play(Create(axes), Create(graph))
        self.play(Write(label))
        self.wait(2)
"""


class TestAcceptsRealScenes:
    def test_a_normal_scene_passes(self):
        assert validate_scene(GOOD) == "ChainRule"

    def test_returns_the_class_name_for_the_renderer(self):
        """Taken from the AST, not a regex, so what renders is what was checked."""
        assert validate_scene(GOOD.replace("ChainRule", "Derivatives")) == "Derivatives"

    @pytest.mark.parametrize(
        "base", ["Scene", "MovingCameraScene", "ThreeDScene", "ZoomedScene"]
    )
    def test_all_scene_bases_are_recognised(self, base):
        src = f"from manim import *\n\nclass S({base}):\n    def construct(self):\n        self.wait()"
        assert validate_scene(src) == "S"

    @pytest.mark.parametrize("mod", sorted(ALLOWED_IMPORTS - {"np"}))
    def test_permitted_imports_are_accepted(self, mod):
        src = f"import {mod}\nfrom manim import Scene\n\nclass S(Scene):\n    def construct(self):\n        pass"
        assert validate_scene(src) == "S"

    def test_maths_helpers_survive(self):
        src = (
            "from manim import Scene\nfrom math import sin, pi\nimport numpy as np\n\n"
            "class S(Scene):\n    def construct(self):\n        _ = np.linspace(0, pi, 10); sin(pi)"
        )
        assert validate_scene(src) == "S"


class TestBlocksReachingOutside:
    @pytest.mark.parametrize(
        "line",
        [
            "import os",
            "import sys",
            "import subprocess",
            "import socket",
            "import shutil",
            "import requests",
            "import pathlib",
            "from os import system",
            "from subprocess import run",
            "import os.path",
        ],
    )
    def test_dangerous_imports_are_refused(self, line):
        src = f"{line}\nfrom manim import Scene\n\nclass S(Scene):\n    def construct(self):\n        pass"
        with pytest.raises(UnsafeSceneError):
            validate_scene(src)

    def test_relative_imports_are_refused(self):
        """A relative import would reach our own application package."""
        src = "from . import config\nfrom manim import Scene\n\nclass S(Scene):\n    def construct(self):\n        pass"
        with pytest.raises(UnsafeSceneError, match="Relative"):
            validate_scene(src)

    @pytest.mark.parametrize(
        "call",
        [
            "eval('1+1')",
            "exec('x=1')",
            "compile('1', '<s>', 'eval')",
            "__import__('os')",
            "open('/etc/passwd')",
            "globals()",
            "locals()",
            "getattr(self, 'x')",
            "setattr(self, 'x', 1)",
            "breakpoint()",
        ],
    )
    def test_dangerous_builtins_are_refused(self, call):
        src = f"from manim import Scene\n\nclass S(Scene):\n    def construct(self):\n        {call}"
        with pytest.raises(UnsafeSceneError):
            validate_scene(src)


class TestBlocksIntrospectionEscapes:
    def test_the_subclasses_walk_is_refused(self):
        """().__class__.__bases__[0].__subclasses__() reaches arbitrary classes
        without ever writing the word 'import'."""
        src = (
            "from manim import Scene\n\nclass S(Scene):\n    def construct(self):\n"
            "        x = ().__class__.__bases__[0].__subclasses__()"
        )
        with pytest.raises(UnsafeSceneError):
            validate_scene(src)

    @pytest.mark.parametrize(
        "expr",
        [
            "self.__dict__",
            "self.construct.__globals__",
            "self.construct.__code__",
            "(lambda: 1).__closure__",
            "type(self).__mro__",
            "self.__class__",
        ],
    )
    def test_introspection_attributes_are_refused(self, expr):
        src = f"from manim import Scene\n\nclass S(Scene):\n    def construct(self):\n        y = {expr}"
        with pytest.raises(UnsafeSceneError):
            validate_scene(src)

    def test_unlisted_dunders_are_also_refused(self):
        """The catch-all matters: an enumerated list cannot stay complete."""
        src = (
            "from manim import Scene\n\nclass S(Scene):\n    def construct(self):\n"
            "        y = self.__sizeof__"
        )
        with pytest.raises(UnsafeSceneError, match="Dunder"):
            validate_scene(src)

    @pytest.mark.parametrize("kw", ["global", "nonlocal"])
    def test_scope_escapes_are_refused(self, kw):
        inner = (
            f"        def f():\n            {kw} zz\n            zz = 1\n"
            if kw == "nonlocal"
            else f"        {kw} zz\n"
        )
        prefix = "zz = 0\n" if kw == "nonlocal" else ""
        src = f"from manim import Scene\n\nclass S(Scene):\n    def construct(self):\n{prefix}{inner}"
        with pytest.raises(UnsafeSceneError):
            validate_scene(src)


class TestStructuralRequirements:
    def test_empty_source_is_refused(self):
        with pytest.raises(UnsafeSceneError, match="empty"):
            validate_scene("")

    def test_whitespace_only_is_refused(self):
        with pytest.raises(UnsafeSceneError, match="empty"):
            validate_scene("   \n\n  ")

    def test_syntax_errors_are_reported_clearly(self):
        with pytest.raises(UnsafeSceneError, match="does not parse"):
            validate_scene("class S(Scene:\n  pass")

    def test_no_scene_class_is_refused(self):
        src = "from manim import Scene\n\ndef helper():\n    return 1"
        with pytest.raises(UnsafeSceneError, match="No Scene subclass"):
            validate_scene(src)

    def test_multiple_scene_classes_are_refused(self):
        """Ambiguity about which class renders is a correctness problem, and
        a second class is a convenient place to hide something."""
        src = (
            "from manim import Scene\n\nclass A(Scene):\n    def construct(self):\n        pass\n"
            "\nclass B(Scene):\n    def construct(self):\n        pass"
        )
        with pytest.raises(UnsafeSceneError, match="found 2"):
            validate_scene(src)

    def test_oversized_source_is_refused(self):
        src = GOOD + "\n" + ("# padding\n" * (MAX_SOURCE_CHARS // 10))
        with pytest.raises(UnsafeSceneError, match="over the"):
            validate_scene(src)

    def test_absurdly_complex_source_is_refused(self):
        body = "\n".join(f"        x{i} = {i} + 1" for i in range(2000))
        src = f"from manim import Scene\n\nclass S(Scene):\n    def construct(self):\n{body}"
        with pytest.raises(UnsafeSceneError):
            validate_scene(src)


class TestNoBypassViaFormatting:
    def test_a_forbidden_call_inside_a_comprehension_is_still_caught(self):
        src = (
            "from manim import Scene\n\nclass S(Scene):\n    def construct(self):\n"
            "        y = [eval(c) for c in ['1']]"
        )
        with pytest.raises(UnsafeSceneError):
            validate_scene(src)

    def test_a_forbidden_import_inside_a_function_is_still_caught(self):
        """Nesting hides it from a line-based check, not from an AST walk."""
        src = (
            "from manim import Scene\n\nclass S(Scene):\n    def construct(self):\n"
            "        def go():\n            import os\n        go()"
        )
        with pytest.raises(UnsafeSceneError):
            validate_scene(src)

    def test_a_forbidden_name_in_a_decorator_is_still_caught(self):
        src = (
            "from manim import Scene\n\nclass S(Scene):\n    def construct(self):\n"
            "        pass\n\n    @eval\n    def other(self):\n        pass"
        )
        with pytest.raises(UnsafeSceneError):
            validate_scene(src)
