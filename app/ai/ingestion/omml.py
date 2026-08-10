"""Convert Word's OMML equation markup into LaTeX.

Word stores equations as Office Math Markup (``m:oMath``) inside the paragraph
XML. ``python-docx`` exposes only ``w:t`` runs through ``paragraph.text``, so
every equation in a lecturer's notes is silently dropped before it ever reaches
the chunker — a worked example arrives as "Example 1: Evaluate the limit below."
followed immediately by "Step 1: Factor the numerator." with the actual
expression missing.

This module walks the OMML tree and emits LaTeX, so equations survive ingestion,
get embedded alongside their surrounding prose, and can be rendered by KaTeX in
the client.

Pure ``lxml`` traversal — no new dependencies.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _m(tag: str) -> str:
    return f"{{{M_NS}}}{tag}"


def _local(tag) -> str:
    """Local name of an lxml tag, tolerating comments/PIs whose tag isn't a str."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


# ── Symbol tables ────────────────────────────────────────────────────────────
# Unicode math characters Word writes literally, mapped to LaTeX commands.

_GREEK = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\epsilon", "ϵ": r"\epsilon", "ζ": r"\zeta", "η": r"\eta",
    "θ": r"\theta", "ϑ": r"\vartheta", "ι": r"\iota", "κ": r"\kappa",
    "λ": r"\lambda", "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi", "π": r"\pi",
    "ϖ": r"\varpi", "ρ": r"\rho", "ϱ": r"\varrho", "σ": r"\sigma",
    "ς": r"\varsigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\phi",
    "ϕ": r"\varphi", "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
    "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Υ": r"\Upsilon",
    "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega",
}

_OPERATORS = {
    "∫": r"\int", "∬": r"\iint", "∭": r"\iiint", "∮": r"\oint",
    "∑": r"\sum", "∏": r"\prod", "∐": r"\coprod",
    "⋂": r"\bigcap", "⋃": r"\bigcup", "⋀": r"\bigwedge", "⋁": r"\bigvee",
    "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div",
    "⋅": r"\cdot", "∙": r"\cdot", "∘": r"\circ", "∗": r"\ast",
    "√": r"\sqrt", "∂": r"\partial", "∇": r"\nabla",
    "∞": r"\infty", "∅": r"\emptyset", "∆": r"\Delta",
}

_RELATIONS = {
    "≤": r"\leq", "≥": r"\geq", "≠": r"\neq", "≈": r"\approx",
    "≡": r"\equiv", "∼": r"\sim", "≅": r"\cong", "∝": r"\propto",
    "≪": r"\ll", "≫": r"\gg", "≐": r"\doteq",
    "∈": r"\in", "∉": r"\notin", "∋": r"\ni",
    "⊂": r"\subset", "⊃": r"\supset", "⊆": r"\subseteq", "⊇": r"\supseteq",
    "∪": r"\cup", "∩": r"\cap", "∧": r"\land", "∨": r"\lor", "¬": r"\neg",
    "∀": r"\forall", "∃": r"\exists", "∄": r"\nexists", "∴": r"\therefore",
    "∵": r"\because",
}

_ARROWS = {
    "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "⇒": r"\Rightarrow", "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow",
    "↦": r"\mapsto", "⟶": r"\longrightarrow", "⟵": r"\longleftarrow",
}

_MISC = {
    "…": r"\ldots", "⋯": r"\cdots", "⋮": r"\vdots", "⋱": r"\ddots",
    "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}", "ℤ": r"\mathbb{Z}",
    "ℚ": r"\mathbb{Q}", "ℂ": r"\mathbb{C}", "ℙ": r"\mathbb{P}",
    "ℓ": r"\ell", "ℏ": r"\hbar", "°": r"^{\circ}", "′": r"'", "″": r"''",
    "∠": r"\angle", "∥": r"\parallel", "⊥": r"\perp",
    " ": " ", " ": r"\,", " ": r"\quad",
}

_SYMBOLS: Dict[str, str] = {**_GREEK, **_OPERATORS, **_RELATIONS, **_ARROWS, **_MISC}

# Characters that are structural in LaTeX and must be escaped when they appear
# as literal text inside an equation.
_ESCAPES = {
    "\\": r"\backslash",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "%": r"\%",
    "_": r"\_",
}

# Function names that should render upright via a LaTeX operator command.
_KNOWN_FUNCTIONS = {
    "sin", "cos", "tan", "csc", "sec", "cot",
    "sinh", "cosh", "tanh", "coth",
    "arcsin", "arccos", "arctan",
    "log", "ln", "lg", "exp", "det", "dim", "ker", "deg",
    "gcd", "hom", "inf", "sup", "max", "min", "arg", "Pr", "lim",
}

# N-ary operators whose limits sit above/below rather than beside.
_NARY_DEFAULT_CHR = "∫"


def _translate_text(text: str, *, math_mode: bool = True) -> str:
    """Map unicode math characters to LaTeX and escape structural characters."""
    out: List[str] = []
    for ch in text:
        if ch in _SYMBOLS:
            out.append(_SYMBOLS[ch] + " ")
        elif math_mode and ch in _ESCAPES:
            out.append(_ESCAPES[ch] + " ")
        else:
            out.append(ch)
    return "".join(out)


def _brace(latex: str) -> str:
    """Wrap an argument in braces.

    Always braces rather than special-casing single characters: ``\\binom`` and
    ``\\frac`` take two arguments, so emitting ``\\binom n k`` would be read as
    ``\\binom{n}{k}`` only by luck and ``\\bar x`` becomes ``\\barx`` once
    whitespace is tidied. ``_tidy`` strips the redundant braces afterwards in
    the one place they are safe to drop (single characters after ``^``/``_``).
    """
    s = latex.strip()
    if not s:
        return "{}"
    # Already a balanced {...} group covering the whole string?
    if s.startswith("{") and s.endswith("}"):
        depth = 0
        for i, ch in enumerate(s):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and i < len(s) - 1:
                    break
        else:
            return s
    return "{" + s + "}"


def _attr_val(node, tag: str) -> Optional[str]:
    """Read ``<m:{tag} m:val="..."/>`` from a properties element."""
    if node is None:
        return None
    child = node.find(_m(tag))
    if child is None:
        return None
    return child.get(_m("val"))


def _children_latex(node, skip: tuple = ()) -> str:
    parts = [
        _convert(child)
        for child in node
        if _local(child.tag) not in skip
    ]
    return "".join(parts)


def _find_latex(node, tag: str) -> str:
    child = node.find(_m(tag))
    return _convert(child) if child is not None else ""


# ── Element handlers ─────────────────────────────────────────────────────────


def _h_text(node) -> str:
    return _translate_text(node.text or "")


def _h_run(node) -> str:
    """``m:r`` — a math run. ``m:rPr/m:sty`` carries bold/italic/plain styling."""
    body = "".join(_convert(c) for c in node if _local(c.tag) not in ("rPr", "nor"))
    style = _attr_val(node.find(_m("rPr")), "sty")
    # Word marks upright (non-italic) text with sty="p"; m:nor means "normal text".
    if style == "p" or node.find(_m("rPr") + "/" + _m("nor")) is not None:
        return r"\mathrm" + _brace(body) if body.strip() else body
    if style == "b":
        return r"\mathbf" + _brace(body) if body.strip() else body
    if style == "bi":
        return r"\boldsymbol" + _brace(body) if body.strip() else body
    return body


def _h_fraction(node) -> str:
    num = _find_latex(node, "num")
    den = _find_latex(node, "den")
    kind = _attr_val(node.find(_m("fPr")), "type")
    if kind == "noBar":  # binomial coefficient
        return r"\binom" + _brace(num) + _brace(den)
    if kind == "lin":
        return f"{_brace(num)}/{_brace(den)}"
    if kind == "skw":
        return f"{_brace(num)}\\!/\\!{_brace(den)}"
    return r"\frac" + _brace(num) + _brace(den)


def _h_radical(node) -> str:
    deg = _find_latex(node, "deg")
    base = _find_latex(node, "e")
    hide = _attr_val(node.find(_m("radPr")), "degHide")
    if deg.strip() and hide not in ("1", "true", "on"):
        return r"\sqrt[" + deg + "]" + _brace(base)
    return r"\sqrt" + _brace(base)


def _h_superscript(node) -> str:
    return _brace(_find_latex(node, "e")) + "^" + _brace(_find_latex(node, "sup"))


def _h_subscript(node) -> str:
    return _brace(_find_latex(node, "e")) + "_" + _brace(_find_latex(node, "sub"))


def _h_subsup(node) -> str:
    return (
        _brace(_find_latex(node, "e"))
        + "_" + _brace(_find_latex(node, "sub"))
        + "^" + _brace(_find_latex(node, "sup"))
    )


def _h_presubsup(node) -> str:
    """``m:sPre`` — sub/superscripts placed before the base."""
    return (
        "{}_" + _brace(_find_latex(node, "sub"))
        + "^" + _brace(_find_latex(node, "sup"))
        + _brace(_find_latex(node, "e"))
    )


def _h_nary(node) -> str:
    """``m:nary`` — integrals, sums, products and friends."""
    props = node.find(_m("naryPr"))
    chars = _attr_val(props, "chr") or _NARY_DEFAULT_CHR
    op = _SYMBOLS.get(chars, _translate_text(chars))

    sub = _find_latex(node, "sub")
    sup = _find_latex(node, "sup")
    body = _find_latex(node, "e")

    # limLoc="undOvr" stacks limits above/below; "subSup" (default for ∫) sits beside.
    lim_loc = _attr_val(props, "limLoc")
    stack = lim_loc == "undOvr"

    out = op.strip()
    if sub.strip():
        out += ("\\limits" if stack else "") + "_" + _brace(sub)
        stack = False  # \limits only needs stating once
    if sup.strip():
        out += ("\\limits" if stack else "") + "^" + _brace(sup)
    return out + " " + body


def _h_delimiter(node) -> str:
    props = node.find(_m("dPr"))
    beg = _attr_val(props, "begChr")
    end = _attr_val(props, "endChr")
    sep = _attr_val(props, "sepChr")
    # Word omits the attribute when it is the default parenthesis.
    beg = "(" if beg is None else beg
    end = ")" if end is None else end
    sep = "|" if sep is None else sep

    inner = [_convert(c) for c in node.findall(_m("e"))]
    joined = (f" \\{sep} " if sep else " ").join(i for i in inner)

    left = _delim_token(beg, left=True)
    right = _delim_token(end, left=False)
    return f"\\left{left} {joined} \\right{right}"


_DELIM_MAP = {
    "": ".", "(": "(", ")": ")", "[": "[", "]": "]",
    "{": r"\{", "}": r"\}", "|": "|", "‖": r"\|",
    "⟨": r"\langle", "⟩": r"\rangle",
    "⌈": r"\lceil", "⌉": r"\rceil", "⌊": r"\lfloor", "⌋": r"\rfloor",
}


def _delim_token(ch: str, *, left: bool) -> str:
    if ch in _DELIM_MAP:
        return _DELIM_MAP[ch]
    return ch or "."


def _h_function(node) -> str:
    """``m:func`` — a named function applied to an argument, e.g. sin(x)."""
    name = _find_latex(node, "fName").strip()
    arg = _find_latex(node, "e")
    bare = re.sub(r"\\mathrm|\\operatorname|[{}\\ ]", "", name)
    if bare in _KNOWN_FUNCTIONS:
        return f"\\{bare} {arg}"
    # A nested m:limLow already emitted a command such as "\lim_{x \to a}";
    # wrapping that in \operatorname{...} would be invalid.
    if name.startswith("\\"):
        return f"{name} {arg}"
    if name:
        return r"\operatorname" + _brace(name) + " " + arg
    return arg


def _h_limlow(node) -> str:
    """``m:limLow`` — limit written below, e.g. lim_{x→a}."""
    base = _find_latex(node, "e").strip()
    lim = _find_latex(node, "lim")
    bare = re.sub(r"\\mathrm|\\operatorname|[{}\\ ]", "", base)
    if bare in _KNOWN_FUNCTIONS:
        base = f"\\{bare}"
    return f"{base}_{_brace(lim)}"


def _h_limupp(node) -> str:
    base = _find_latex(node, "e").strip()
    lim = _find_latex(node, "lim")
    return f"{base}^{_brace(lim)}"


_ACCENT_MAP = {
    "̂": r"\hat", "^": r"\hat",
    "̅": r"\bar", "‾": r"\bar", "¯": r"\bar",
    "̃": r"\tilde", "~": r"\tilde",
    "̇": r"\dot", "̈": r"\ddot",
    "⃗": r"\vec", "→": r"\vec",
    "̆": r"\breve", "̌": r"\check", "́": r"\acute", "̀": r"\grave",
}


def _h_accent(node) -> str:
    chars = _attr_val(node.find(_m("accPr")), "chr") or "̂"
    cmd = _ACCENT_MAP.get(chars, r"\hat")
    return cmd + _brace(_find_latex(node, "e"))


def _h_bar(node) -> str:
    pos = _attr_val(node.find(_m("barPr")), "pos")
    cmd = r"\underline" if pos == "bot" else r"\overline"
    return cmd + _brace(_find_latex(node, "e"))


def _h_groupchr(node) -> str:
    chars = _attr_val(node.find(_m("groupChrPr")), "chr") or "⏟"
    body = _brace(_find_latex(node, "e"))
    if chars == "⏞":
        return r"\overbrace" + body
    return r"\underbrace" + body


def _h_matrix(node) -> str:
    rows: List[str] = []
    for row in node.findall(_m("mr")):
        cells = [_convert(e) for e in row.findall(_m("e"))]
        rows.append(" & ".join(c.strip() for c in cells))
    body = " \\\\ ".join(rows)
    return r"\begin{matrix} " + body + r" \end{matrix}"


def _h_eqarr(node) -> str:
    rows = [_convert(e) for e in node.findall(_m("e"))]
    body = " \\\\ ".join(r.strip() for r in rows)
    return r"\begin{aligned} " + body + r" \end{aligned}"


def _h_box(node) -> str:
    return _find_latex(node, "e")


def _h_borderbox(node) -> str:
    return r"\boxed" + _brace(_find_latex(node, "e"))


def _h_phantom(node) -> str:
    return r"\phantom" + _brace(_find_latex(node, "e"))


def _h_passthrough(node) -> str:
    return _children_latex(node)


def _h_ignore(node) -> str:
    return ""


_HANDLERS: Dict[str, Callable] = {
    "t": _h_text,
    "r": _h_run,
    "f": _h_fraction,
    "rad": _h_radical,
    "sSup": _h_superscript,
    "sSub": _h_subscript,
    "sSubSup": _h_subsup,
    "sPre": _h_presubsup,
    "nary": _h_nary,
    "d": _h_delimiter,
    "func": _h_function,
    "limLow": _h_limlow,
    "limUpp": _h_limupp,
    "acc": _h_accent,
    "bar": _h_bar,
    "groupChr": _h_groupchr,
    "m": _h_matrix,
    "eqArr": _h_eqarr,
    "box": _h_box,
    "borderBox": _h_borderbox,
    "phant": _h_phantom,
    # Containers whose children are simply concatenated.
    "oMath": _h_passthrough,
    "oMathPara": _h_passthrough,
    "e": _h_passthrough,
    "num": _h_passthrough,
    "den": _h_passthrough,
    "sup": _h_passthrough,
    "sub": _h_passthrough,
    "deg": _h_passthrough,
    "lim": _h_passthrough,
    "fName": _h_passthrough,
    "mr": _h_passthrough,
    # Property elements carry formatting we read via attributes, never text.
    "fPr": _h_ignore,
    "radPr": _h_ignore,
    "naryPr": _h_ignore,
    "dPr": _h_ignore,
    "accPr": _h_ignore,
    "barPr": _h_ignore,
    "groupChrPr": _h_ignore,
    "mPr": _h_ignore,
    "eqArrPr": _h_ignore,
    "boxPr": _h_ignore,
    "borderBoxPr": _h_ignore,
    "sSupPr": _h_ignore,
    "sSubPr": _h_ignore,
    "sSubSupPr": _h_ignore,
    "sPrePr": _h_ignore,
    "funcPr": _h_ignore,
    "limLowPr": _h_ignore,
    "limUppPr": _h_ignore,
    "phantPr": _h_ignore,
    "rPr": _h_ignore,
    "ctrlPr": _h_ignore,
    "argPr": _h_ignore,
    "mcs": _h_ignore,
    "mcPr": _h_ignore,
}


def _convert(node) -> str:
    tag = _local(node.tag)
    if not tag:
        return ""
    handler = _HANDLERS.get(tag)
    if handler is not None:
        return handler(node)
    # Unknown OMML construct: recurse so its text is not lost.
    if node.tag == f"{{{W_NS}}}t":
        return _translate_text(node.text or "")
    return _children_latex(node)


_WS_RE = re.compile(r"[ \t]{2,}")


def _tidy(latex: str) -> str:
    out = _WS_RE.sub(" ", latex.replace("\n", " ")).strip()
    # "\alpha x" needs its space, "\frac {a}" does not.
    out = re.sub(r"(\\[a-zA-Z]+) +(?=[{^_])", r"\1", out)
    # Drop braces that _brace added defensively but that LaTeX does not need:
    # a single character carrying a script, or a single-character script.
    out = re.sub(r"\{([A-Za-z0-9])\}(?=[_^])", r"\1", out)
    out = re.sub(r"([_^])\{([A-Za-z0-9])\}", r"\1\2", out)
    return out.strip()


def omml_to_latex(element) -> str:
    """Convert an ``m:oMath`` / ``m:oMathPara`` element to a LaTeX string.

    Returns an empty string when the element yields no content, so callers can
    skip empty equation placeholders. Never raises: a conversion failure
    degrades to empty rather than aborting ingestion of a whole document.
    """
    if element is None:
        return ""
    try:
        return _tidy(_convert(element))
    except Exception:  # pragma: no cover - defensive
        logger.warning("OMML conversion failed; equation dropped", exc_info=True)
        return ""


def iter_omml(parent) -> List:
    """Return every ``m:oMath`` element beneath ``parent`` in document order."""
    return parent.findall(f".//{_m('oMath')}")


def paragraph_has_math(paragraph) -> bool:
    """True when a ``python-docx`` paragraph contains at least one equation."""
    return bool(iter_omml(paragraph._p))
