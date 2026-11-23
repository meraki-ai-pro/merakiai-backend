"""Algebraic equivalence for typed maths answers.

A key of ``2x+1`` and a student's ``1 + 2x`` are the same answer; text
comparison marks the student wrong. This decides equivalence by value, so the
mark comes from mathematics rather than from a model or from spelling.

Student input is untrusted and ``sympy.parse_expr`` is built on ``eval``, so
nothing reaches it until it has passed a character allowlist and every name in
it is a single-letter variable or a known function. Equivalence is then tested
by evaluating both sides at a few sample points instead of ``simplify`` —
simplify can run for minutes on adversarial input, and sampling cannot.
"""

from __future__ import annotations

import random
import re
import string

_MAX_LEN = 120
_ALLOWED = re.compile(r"^[0-9a-zA-Z+\-*/^().\s=]+$")
_FUNCS = {"sin", "cos", "tan", "exp", "log", "ln", "sqrt", "pi"}
_NAME = re.compile(r"[a-zA-Z]+")
_MAX_EXPONENT = 50

_LATEX = [
    (re.compile(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}"), r"((\1)/(\2))"),
    (re.compile(r"\\sqrt\{([^{}]*)\}"), r"sqrt(\1)"),
    (re.compile(r"\\(cdot|times)"), "*"),
    (re.compile(r"\\left|\\right|\\,|\\!|\\;"), ""),
    (re.compile(r"\\(sin|cos|tan|exp|log|ln|pi)"), r" \1 "),
]


def _from_latex(text: str) -> str:
    text = text.replace("$", "")
    for pattern, repl in _LATEX:
        text = pattern.sub(repl, text)
    return text.replace("{", "(").replace("}", ")")


def _parse(text: str):
    """Parse one side, or return None when it is not safe, plain algebra."""
    from sympy import E, Pow, Symbol
    from sympy.parsing.sympy_parser import (
        convert_xor, implicit_multiplication_application, parse_expr,
        standard_transformations,
    )

    if len(text) > _MAX_LEN or not text.strip() or not _ALLOWED.match(text):
        return None
    for name in _NAME.findall(text):
        # Implicit multiplication would read "ab" as a*b, so "mean" or "mode"
        # would parse as a product of letters. Only single letters and known
        # functions are allowed, which also keeps attribute tricks out.
        if len(name) > 1 and name.lower() not in _FUNCS:
            return None
    text = re.sub(r"\bln\b", "log", text)
    # Every letter is a plain symbol — otherwise "N", "S" or "Q" resolve to
    # SymPy objects — except e and E, which students mean as Euler's number.
    local = {c: Symbol(c) for c in string.ascii_letters if c not in "eEI"}
    local["e"] = E
    try:
        expr = parse_expr(
            text,
            local_dict=local,
            transformations=standard_transformations
            + (implicit_multiplication_application, convert_xor),
            evaluate=False,
        )
    except Exception:  # noqa: BLE001 — anything unparseable is simply not algebra
        return None
    # 9^9^9 would be evaluated in full at the first sample point, so a power
    # may not sit inside an exponent. Unevaluated division is x*y^-1, which is
    # the one inner power allowed — x^(1/2) must still parse.
    for node in expr.atoms(Pow):
        if any(inner.exp != -1 for inner in node.exp.atoms(Pow)):
            return None
        if node.exp.is_number and abs(float(node.exp)) > _MAX_EXPONENT:
            return None
    return expr


def _same_value(a, b) -> bool:
    symbols = sorted(a.free_symbols | b.free_symbols, key=str)
    rng = random.Random(0)  # deterministic: the same answer always gets the same mark
    agreed = 0
    for _ in range(8):
        point = {s: rng.uniform(0.3, 2.7) for s in symbols}
        try:
            va = complex(a.evalf(subs=point))
            vb = complex(b.evalf(subs=point))
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            continue
        if any(v != v or abs(v) == float("inf") for v in (va, vb)):
            continue
        if abs(va - vb) > 1e-7 * max(1.0, abs(va), abs(vb)):
            return False
        agreed += 1
    return agreed >= 3


def equivalent(expected: str, given: str) -> bool:
    """True when both answers are algebra and agree in value.

    Equations (one ``=``) compare side by side. Anything that does not parse
    returns False, and the caller's text comparison stands.
    """
    left, right = _from_latex(expected), _from_latex(given)
    if left.count("=") != right.count("=") or left.count("=") > 1:
        return False
    try:
        pairs = list(zip(left.split("="), right.split("=")))
        parsed = [(_parse(x), _parse(y)) for x, y in pairs]
        if any(x is None or y is None for x, y in parsed):
            return False
        return all(_same_value(x, y) for x, y in parsed)
    except Exception:  # noqa: BLE001 — a marking helper must never 500 a submission
        return False


if __name__ == "__main__":
    assert equivalent("2x+1", "1 + 2x")
    assert equivalent("$\\frac{1}{2}x^2$", "x^2/2")
    assert equivalent("y = 2x+1", "y=1+2*x")
    assert not equivalent("2x+1", "2x-1")
    assert not equivalent("mean", "mode")
    assert not equivalent("x", "__import__('os')")
    assert not equivalent("9^9^9^9", "1")
    print("ok")
