"""Structure- and maths-preserving document parser.

Replaces the ``unstructured``-based parser for ingestion. Three things the old
parser lost, all of which matter for a Calculus / Statistics course:

1. **Equations.** ``python-docx``'s ``paragraph.text`` skips ``m:oMath``, so
   every Word equation was dropped. Here they are converted to LaTeX by
   :mod:`app.ai.ingestion.omml` and kept inline with the prose that explains
   them.
2. **Structure.** The old parser flattened a document into ``{title, content}``
   sections joined by single newlines, which left the chunker with nothing to
   split on. Here each block keeps its type, heading path, and document order.
3. **Provenance.** Nothing recorded where a passage came from, so citations were
   impossible. Every block carries its page (PDF) and heading path (both), which
   the chunker promotes into vector metadata.

Output is a flat list of :class:`Block` dicts in document order.
"""

from __future__ import annotations

import io
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional

from app.ai.ingestion.omml import omml_to_latex

logger = logging.getLogger(__name__)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# Block types
HEADING = "heading"
PARAGRAPH = "paragraph"
EQUATION = "equation"
LIST_ITEM = "list_item"
TABLE = "table"
FIGURE = "figure"

Block = Dict[str, Any]

# Paragraph-level XML we must not walk into when collecting text: these hold
# formatting, not content.
_SKIP_TAGS = {
    "pPr", "rPr", "sectPr", "numPr", "tblPr", "tcPr", "trPr",
    "proofErr", "bookmarkEnd", "lastRenderedPageBreak", "commentRangeStart",
    "commentRangeEnd", "commentReference", "footnotePr", "endnotePr",
}


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _escape_markdown_math(text: str) -> str:
    """Escape ``$`` so literal currency is not read as a math delimiter."""
    return text.replace("$", r"\$")


# ── DOCX ─────────────────────────────────────────────────────────────────────


def _collect_paragraph_parts(element, parts: List[tuple]) -> None:
    """Walk a ``w:p`` subtree in document order, keeping text and maths interleaved.

    Emits ``("text", str)``, ``("inline", latex)`` and ``("display", latex)``
    tuples. Order matters: "the derivative of $x^2$ is $2x$" must not collapse
    into "the derivative of is" with the maths appended somewhere else.
    """
    for child in element:
        tag = _local(child.tag)
        if not tag or tag in _SKIP_TAGS:
            continue

        if tag == "oMathPara":
            for equation in child.findall(f"{{{M_NS}}}oMath"):
                latex = omml_to_latex(equation)
                if latex:
                    parts.append(("display", latex))
        elif tag == "oMath":
            latex = omml_to_latex(child)
            if latex:
                parts.append(("inline", latex))
        elif tag == "t":
            parts.append(("text", child.text or ""))
        elif tag == "tab":
            parts.append(("text", " "))
        elif tag in ("br", "cr"):
            parts.append(("text", "\n"))
        elif tag == "drawing":
            parts.append(("figure", _drawing_description(child)))
        else:
            _collect_paragraph_parts(child, parts)


def _drawing_description(drawing) -> str:
    """Pull alt text off an embedded image so its position is not lost."""
    for prop in drawing.iter():
        if _local(prop.tag) in ("docPr", "cNvPr"):
            descr = prop.get("descr") or prop.get("name") or ""
            if descr:
                return descr.strip()
    return "image"


def _render_parts(parts: Iterable[tuple]) -> tuple[str, List[str], bool]:
    """Turn collected parts into markdown text plus the LaTeX found in them."""
    out: List[str] = []
    equations: List[str] = []
    display_only = True

    for kind, value in parts:
        if kind == "text":
            if value.strip():
                display_only = False
            out.append(_escape_markdown_math(value))
        elif kind == "inline":
            equations.append(value)
            out.append(f"${value}$")
        elif kind == "display":
            equations.append(value)
            out.append(f"\n\n$$\n{value}\n$$\n\n")
        elif kind == "figure":
            display_only = False
            out.append(f"![{value}](figure)")

    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, equations, display_only and bool(equations)


_HEADING_LEVEL_RE = re.compile(r"(\d+)")


def _heading_level(paragraph) -> Optional[int]:
    """Return the outline level of a paragraph, or None if it is body text."""
    try:
        style = paragraph.style.name or ""
    except Exception:
        style = ""

    if style.startswith("Heading"):
        match = _HEADING_LEVEL_RE.search(style)
        return int(match.group(1)) if match else 1
    if style in ("Title", "Subtitle"):
        return 1 if style == "Title" else 2

    # Some documents set an outline level without using a Heading style.
    outline = paragraph._p.find(f"{{{W_NS}}}pPr/{{{W_NS}}}outlineLvl")
    if outline is not None:
        val = outline.get(f"{{{W_NS}}}val")
        if val is not None and val.isdigit() and int(val) < 9:
            return int(val) + 1
    return None


def _is_list_item(paragraph) -> bool:
    if paragraph._p.find(f"{{{W_NS}}}pPr/{{{W_NS}}}numPr") is not None:
        return True
    # "List Bullet" / "List Number" carry their numbering on the style rather
    # than on the paragraph, so numPr alone misses them.
    try:
        return (paragraph.style.name or "").startswith("List")
    except Exception:
        return False


def _table_to_markdown(table) -> tuple[str, bool]:
    """Render a table as markdown, converting any equations inside its cells.

    The previous pipeline turned tables into prose ("normalize_sections"), which
    destroyed the row/column relationships that make a distribution table or a
    formula sheet useful.
    """
    rows: List[List[str]] = []
    has_math = False

    for row in table.rows:
        cells: List[str] = []
        for cell in row.cells:
            cell_parts: List[tuple] = []
            for paragraph in cell.paragraphs:
                _collect_paragraph_parts(paragraph._p, cell_parts)
                cell_parts.append(("text", " "))
            text, equations, _ = _render_parts(cell_parts)
            if equations:
                has_math = True
            cells.append(text.replace("\n", " ").replace("|", r"\|").strip())
        if any(cells):
            rows.append(cells)

    if not rows:
        return "", False

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    header, *body = rows
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines), has_math


def parse_docx_blocks(source) -> List[Block]:
    """Parse a .docx into ordered blocks, preserving equations and structure."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(source)
    body = document.element.body

    blocks: List[Block] = []
    heading_path: List[str] = []
    index = 0

    for element in body:
        tag = _local(element.tag)

        if tag == "p":
            paragraph = Paragraph(element, document)
            parts: List[tuple] = []
            _collect_paragraph_parts(element, parts)
            text, equations, equation_only = _render_parts(parts)
            if not text:
                continue

            level = _heading_level(paragraph)
            if level is not None:
                heading_path = heading_path[: level - 1]
                heading_path.append(text)
                blocks.append({
                    "type": HEADING,
                    "text": text,
                    "level": level,
                    "heading_path": list(heading_path[:-1]),
                    "page": None,
                    "equations": equations,
                    "has_math": bool(equations),
                    "index": index,
                })
            else:
                if equation_only:
                    block_type = EQUATION
                    # Word marks a standalone equation with a bare m:oMath as
                    # often as with m:oMathPara. A paragraph that is nothing but
                    # maths is display maths either way, so promote it rather
                    # than leaving it inline mid-line.
                    text = "$$\n" + "\n".join(equations) + "\n$$"
                elif _is_list_item(paragraph):
                    block_type = LIST_ITEM
                else:
                    block_type = PARAGRAPH
                blocks.append({
                    "type": block_type,
                    "text": text,
                    "level": None,
                    "heading_path": list(heading_path),
                    "page": None,
                    "equations": equations,
                    "has_math": bool(equations),
                    "index": index,
                })
            index += 1

        elif tag == "tbl":
            markdown, has_math = _table_to_markdown(Table(element, document))
            if not markdown:
                continue
            blocks.append({
                "type": TABLE,
                "text": markdown,
                "level": None,
                "heading_path": list(heading_path),
                "page": None,
                "equations": [],
                "has_math": has_math,
                "index": index,
            })
            index += 1

    return blocks


# ── PDF ──────────────────────────────────────────────────────────────────────

# A PDF text layer stores glyphs, not semantics, so an equation typeset by LaTeX
# comes out as mangled runs like "f (x) = x2 1". We keep the text (it is still
# searchable prose) and flag the block so downstream code can warn, rather than
# pretending the maths survived. Set MATH_OCR_PROVIDER to route these through a
# real formula OCR service when one is configured.
_MATH_GLYPH_RE = re.compile(r"[∫∑∏√±≤≥≠≈∞∂∇αβγδθλμσπΣΔΩ]")
# Below this many characters a page has no usable text layer — treat it as a scan.
_MIN_PAGE_TEXT_CHARS = 40
_EQUATION_HINT_RE = re.compile(r"[a-zA-Z]\s*[=<>]\s*[-+]?[\w\\(]|\bd[xyt]\b|\^|_\{")


def _looks_mathematical(text: str) -> bool:
    return bool(_MATH_GLYPH_RE.search(text) or _EQUATION_HINT_RE.search(text))


_PDF_HEADING_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+\S|CHAPTER\b|SECTION\b|Chapter\b|Section\b)"
)


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 90:
        return False
    if _PDF_HEADING_RE.match(stripped):
        return True
    # A short line in title case or caps with no terminal punctuation.
    if len(stripped.split()) <= 10 and not stripped.endswith((".", ",", ";", ":")):
        letters = [c for c in stripped if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
            return True
    return False


def _boilerplate_fingerprint(line: str) -> str:
    """Normalise a line so running headers/footers match across pages.

    Page furniture varies only by number and is often letter-spaced by the PDF
    text layer ("1 | P a g e"), so digits are collapsed to ``#`` and all
    whitespace is removed before comparing.
    """
    return re.sub(r"\s+", "", re.sub(r"\d+", "#", line)).lower()


def _find_boilerplate(page_texts: List[str]) -> set:
    """Identify running headers and footers so they do not become headings."""
    from collections import Counter

    if len(page_texts) < 2:
        return set()

    counts: Counter = Counter()
    for text in page_texts:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # On a sparse page every line is content — there is no furniture to
        # separate from the body, and guessing costs us real material.
        if len(lines) < 4:
            continue
        # Furniture sits on the outermost line at either edge, and is short.
        for line in (lines[0], lines[-1]):
            if len(line) <= 80:
                counts[_boilerplate_fingerprint(line)] += 1

    threshold = max(2, int(len(page_texts) * 0.3))
    return {fp for fp, n in counts.items() if n >= threshold and fp}


def parse_pdf_blocks(source) -> List[Block]:
    """Parse a PDF into ordered blocks, one group per paragraph, page numbers kept."""
    from pypdf import PdfReader

    reader = PdfReader(source)
    blocks: List[Block] = []
    heading_path: List[str] = []
    index = 0

    page_texts: List[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            page_texts.append(page.extract_text() or "")
        except Exception:
            logger.warning("Failed to extract text from page %s", page_number, exc_info=True)
            page_texts.append("")

    boilerplate = _find_boilerplate(page_texts)

    for page_number, raw in enumerate(page_texts, start=1):
        # A page with (almost) no text layer is a scan or an image-only export —
        # a lecturer's photographed or scanned handwritten notes look exactly
        # like this. Without a marker the page would vanish silently, so emit a
        # placeholder that routes it to OCR instead.
        if len(raw.strip()) < _MIN_PAGE_TEXT_CHARS:
            blocks.append({
                "type": PARAGRAPH,
                "text": "",
                "level": None,
                "heading_path": list(heading_path),
                "page": page_number,
                "equations": [],
                "has_math": False,
                "needs_math_ocr": True,
                "is_image_page": True,
                "index": index,
            })
            index += 1
            continue

        raw = "\n".join(
            ln for ln in raw.splitlines()
            if _boilerplate_fingerprint(ln) not in boilerplate
        )
        # Join hyphenated line breaks, then group into paragraphs on blank lines.
        raw = re.sub(r"-\n(?=[a-z])", "", raw)
        for chunk in re.split(r"\n\s*\n", raw):
            lines = [ln.rstrip() for ln in chunk.splitlines() if ln.strip()]
            if not lines:
                continue

            if len(lines) == 1 and _looks_like_heading(lines[0]):
                heading = lines[0].strip()
                heading_path = [heading]
                blocks.append({
                    "type": HEADING,
                    "text": heading,
                    "level": 1,
                    "heading_path": [],
                    "page": page_number,
                    "equations": [],
                    "has_math": False,
                    "index": index,
                })
                index += 1
                continue

            text = " ".join(lines).strip()
            if not text:
                continue
            blocks.append({
                "type": PARAGRAPH,
                "text": _escape_markdown_math(text),
                "level": None,
                "heading_path": list(heading_path),
                "page": page_number,
                "equations": [],
                "has_math": False,
                "needs_math_ocr": _looks_mathematical(text),
                "index": index,
            })
            index += 1

    return blocks


# ── Plain text / markdown ────────────────────────────────────────────────────

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\$([^$\n]+)\$", re.DOTALL)


def parse_text_blocks(source) -> List[Block]:
    """Parse .md / .txt. LaTeX written as $…$ or $$…$$ is kept as-is."""
    data = source.read() if hasattr(source, "read") else source
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")

    blocks: List[Block] = []
    heading_path: List[str] = []
    index = 0

    for chunk in re.split(r"\n\s*\n", data):
        text = chunk.strip()
        if not text:
            continue

        heading = _MD_HEADING_RE.match(text)
        if heading and "\n" not in text:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            heading_path = heading_path[: level - 1]
            heading_path.append(title)
            blocks.append({
                "type": HEADING, "text": title, "level": level,
                "heading_path": list(heading_path[:-1]), "page": None,
                "equations": [], "has_math": False, "index": index,
            })
            index += 1
            continue

        equations = [m.group(1) or m.group(2) for m in _MD_MATH_RE.finditer(text)]
        blocks.append({
            "type": PARAGRAPH, "text": text, "level": None,
            "heading_path": list(heading_path), "page": None,
            "equations": [e.strip() for e in equations if e],
            "has_math": bool(equations), "index": index,
        })
        index += 1

    return blocks


# ── Entry point ──────────────────────────────────────────────────────────────

_PARSERS = {
    "docx": parse_docx_blocks,
    "docm": parse_docx_blocks,
    "pdf": parse_pdf_blocks,
    "md": parse_text_blocks,
    "markdown": parse_text_blocks,
    "txt": parse_text_blocks,
}

SUPPORTED_EXTENSIONS = tuple(sorted(_PARSERS))


def parse_blocks(content: bytes, filename: str) -> List[Block]:
    """Parse raw file bytes into ordered blocks.

    Raises ``ValueError`` for unsupported extensions so ingestion can mark the
    document failed with a message the admin UI can show.
    """
    suffix = os.path.splitext(filename)[1].lstrip(".").lower()
    parser = _PARSERS.get(suffix)
    if parser is None:
        raise ValueError(
            f"Unsupported file type {suffix!r}. Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
        )

    blocks = parser(io.BytesIO(content))
    logger.info(
        "Parsed %s: %d blocks, %d containing maths",
        filename, len(blocks), sum(1 for b in blocks if b.get("has_math")),
    )
    return blocks


def blocks_needing_math_ocr(blocks: List[Block]) -> List[Block]:
    """PDF blocks that look mathematical but whose maths did not survive extraction."""
    return [b for b in blocks if b.get("needs_math_ocr")]
