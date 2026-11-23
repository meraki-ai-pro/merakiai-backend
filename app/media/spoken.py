"""Server-side twin of the client's lib/speech.ts toSpokenText.

Narration text used to be built in the browser and sent up, which made the
endpoint a way to voice any sentence in a lecturer's cloned voice. The server
now derives the spoken text itself from markdown it has verified came from the
tutor, so the client's text is never trusted. Keep this in step with
merakiai-frontend/src/lib/speech.ts (wording differences only change how a slide
sounds, never what can be voiced).
"""

from __future__ import annotations

import re

_R: list[tuple[str, str]] = [
    (r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r" \1 over \2 "),
    (r"\\dfrac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r" \1 over \2 "),
    (r"\\binom\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r" \1 choose \2 "),
    (r"\\sqrt\s*\[\s*3\s*\]\s*\{([^{}]+)\}", r" the cube root of \1 "),
    (r"\\sqrt\s*\{([^{}]+)\}", r" the square root of \1 "),
    (r"\\int_\s*\{?([^{}\s]+)\}?\s*\^\s*\{?([^{}\s]+)\}?", r" the integral from \1 to \2 of "),
    (r"\\sum_\s*\{?([^{}]+)\}?\s*\^\s*\{?([^{}\s]+)\}?", r" the sum from \1 to \2 of "),
    (r"\\lim_\s*\{([^{}]+)\}", r" the limit as \1 of "),
    (r"\\int", " the integral of "),
    (r"\\sum", " the sum of "),
    (r"\\prod", " the product of "),
    (r"\^\s*\{?2\}?", " squared "),
    (r"\^\s*\{?3\}?", " cubed "),
    (r"\^\s*\{([^{}]+)\}", r" to the power of \1 "),
    (r"\^\s*([A-Za-z0-9])", r" to the power of \1 "),
    (r"_\s*\{([^{}]+)\}", r" sub \1 "),
    (r"_\s*([A-Za-z0-9])", r" sub \1 "),
    (r"\\leq|\\le\b", " is less than or equal to "),
    (r"\\geq|\\ge\b", " is greater than or equal to "),
    (r"\\neq|\\ne\b", " is not equal to "),
    (r"\\approx", " is approximately "),
    (r"\\times", " times "),
    (r"\\cdot", " times "),
    (r"\\div", " divided by "),
    (r"\\pm", " plus or minus "),
    (r"\\to|\\rightarrow", " approaches "),
    (r"\\infty", " infinity "),
    (r"\\partial", " partial "),
    (r"\\pi\b", " pi "),
    (r"\\theta\b", " theta "),
    (r"\\alpha\b", " alpha "),
    (r"\\beta\b", " beta "),
    (r"\\mu\b", " mu "),
    (r"\\sigma\b", " sigma "),
    (r"\\lambda\b", " lambda "),
    (r"\\delta\b", " delta "),
    (r"\\Delta\b", " delta "),
    (r"\\in\b", " is in "),
    (r"\\ldots|\\cdots|\\dots", " and so on "),
    (r"\\(sin|cos|tan|log|ln|exp|max|min|det|lim)\b", r" \1 "),
    (r"\\left|\\right", " "),
    (r"\\operatorname\s*\{([^{}]+)\}", r" \1 "),
    (r"\\mathrm\s*\{([^{}]+)\}", r" \1 "),
    (r"\\mathbb\s*\{R\}", " the real numbers "),
    (r"\\mathbb\s*\{N\}", " the natural numbers "),
    (r"\\mathbb\s*\{Z\}", " the integers "),
    (r"\\[a-zA-Z]+\s*\{([^{}]*)\}", r" \1 "),
    (r"\\[a-zA-Z]+", " "),
    (r"[{}]", " "),
    (r"\\\\", ". "),
    (r"\\,|\;|\\!|\\:", " "),
]
_REPLACEMENTS = [(re.compile(p), r) for p, r in _R]

_CITATION = re.compile(r"\[(\d{1,2})\](?!\()")
_PROTECTED = re.compile(
    r"```[\s\S]*?```|`[^`\n]*`|\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\[[^\]]*\]\([^)]*\)"
)


def _strip_citations(md: str) -> str:
    out, cursor = [], 0
    for m in _PROTECTED.finditer(md):
        out.append(_CITATION.sub("", md[cursor:m.start()]))
        out.append(m.group(0))
        cursor = m.end()
    out.append(_CITATION.sub("", md[cursor:]))
    return "".join(out)


def _latex_to_words(latex: str) -> str:
    text = latex
    for pat, rep in _REPLACEMENTS:
        text = pat.sub(rep, text)
    text = text.replace("=", " equals ").replace("+", " plus ")
    text = re.sub(r"(?<=[\w\s)])-(?=[\w\s(])", " minus ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def to_spoken_text(markdown: str) -> str:
    text = _strip_citations(markdown)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"\$\$([\s\S]+?)\$\$", lambda m: f" {_latex_to_words(m.group(1))} ", text)
    text = re.sub(r"\$([^$\n]+)\$", lambda m: f" {_latex_to_words(m.group(1))} ", text)
    text = re.sub(r"(^\|.*\|[ \t]*$\n?)+", " See the table on the board. ", text, flags=re.M)
    text = re.sub(
        r"!\[([^\]]*)\]\([^)]*\)",
        lambda m: f" See the figure: {m.group(1)}. " if m.group(1) else " See the figure on the board. ",
        text,
    )
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"(\*\*|__|\*|_|`)", "", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.M)
    text = text.replace("\\$", "dollars ")
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\n{2,}", ". ", text)
    text = text.replace("\n", " ")
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    text = re.sub(r"\.{2,}", ".", text)
    return text.strip()


def spoken_answer(markdown: str) -> str:
    """Board fences and plot/video JSON are for the eye, not the ear."""
    text = re.sub(r"^:::\s*(plot|video)\b[\s\S]*?^:::\s*$", " ", markdown, flags=re.M)
    text = re.sub(r"^:::.*$", " ", text, flags=re.M)
    return to_spoken_text(text)


def chunk_for_speech(text: str, max_chars: int = 1150) -> list[str]:
    """Split at sentence ends so no chunk stops mid-sentence."""
    sentences = re.findall(r"[^.!?]+[.!?]+[\"')\]]*\s*|[^.!?]+$", text) or [text]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current + sentence) > max_chars and current:
            chunks.append(current.strip())
            current = ""
        for i in range(0, len(sentence), max_chars):
            piece = sentence[i:i + max_chars]
            if len(current + piece) > max_chars and current:
                chunks.append(current.strip())
                current = ""
            current += piece
    if current.strip():
        chunks.append(current.strip())
    return chunks
