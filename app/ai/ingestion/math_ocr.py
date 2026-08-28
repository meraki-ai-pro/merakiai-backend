"""Recover mathematics that a PDF text layer cannot represent.

A PDF stores glyph placements, not semantics. A typeset integral extracts as
runs like ``Z 1 0 x2 dx`` — LaTeX's Computer Modern draws ``∫`` with a glyph the
text layer reports as ``Z``. No amount of parsing fixes that, because the
information is gone before parsing starts. Word documents are unaffected:
equations there are OMML and are converted losslessly by
:mod:`app.ai.ingestion.omml`.

So this stage renders only the pages the parser flagged, asks a vision model to
transcribe them to markdown + LaTeX, and re-parses the result into blocks. It
sits between parsing and chunking so the recovered maths flows through the
normal chunker with its page numbers and heading path intact.

Configuration
-------------
``MATH_OCR_PROVIDER``   ``auto`` (default) | ``anthropic`` | ``mathpix`` | ``none``
``MATH_OCR_MODEL``      vision model id (default ``claude-opus-5``)
``MATH_OCR_MAX_PAGES``  safety cap on pages OCR'd per document (default 40)
``MATH_OCR_DPI``        render resolution (default 200)

With no provider configured the stage is a no-op: documents ingest exactly as
before, with the affected blocks still flagged so an admin can see what was
degraded rather than discovering garbled formulas at question time.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

from app.ai.anthropic_config import client_options

logger = logging.getLogger(__name__)

Block = Dict[str, Any]

_DEFAULT_MODEL = "claude-opus-5"
_MAX_CONCURRENCY = 4

_TRANSCRIBE_PROMPT = """Transcribe this page from a mathematics textbook or lecture notes into Markdown.

Rules:
- Write ALL mathematics as LaTeX: `$...$` inline, `$$...$$` for displayed equations.
- Transcribe formulas exactly as they appear. Do not simplify, solve, correct, or re-derive anything.
- Preserve headings (`#`, `##`), numbered/bulleted lists, and worked-example step structure.
- Render tables as Markdown tables.
- For a figure, diagram, or plot, emit a single line: `![<one-line description of what it shows>](figure)`
- Preserve the reading order of the page.
- Ignore running headers, footers, and page numbers.
- Output only the transcription. No preamble, no commentary, no code fences around the whole page."""


def _provider_name() -> str:
    configured = os.getenv("MATH_OCR_PROVIDER", "auto").strip().lower()
    if configured != "auto":
        return configured
    if os.getenv("MATHPIX_APP_ID") and os.getenv("MATHPIX_APP_KEY"):
        return "mathpix"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "none"


# ── Page rendering ───────────────────────────────────────────────────────────


def _render_pages(content: bytes, page_numbers: Sequence[int]) -> Dict[int, bytes]:
    """Render the given 1-based pages to PNG bytes."""
    import pypdfium2 as pdfium

    dpi = int(os.getenv("MATH_OCR_DPI", "200"))
    scale = dpi / 72.0

    images: Dict[int, bytes] = {}
    pdf = pdfium.PdfDocument(io.BytesIO(content))
    try:
        for page_number in page_numbers:
            index = page_number - 1
            if index < 0 or index >= len(pdf):
                continue
            try:
                bitmap = pdf[index].render(scale=scale)
                buffer = io.BytesIO()
                bitmap.to_pil().save(buffer, format="PNG")
                images[page_number] = buffer.getvalue()
            except Exception:
                logger.warning("Failed to render page %s for OCR", page_number, exc_info=True)
    finally:
        pdf.close()

    return images


# ── Providers ────────────────────────────────────────────────────────────────


async def _transcribe_anthropic(image: bytes) -> Optional[str]:
    """Transcribe a page image with a Claude vision model."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(**client_options())
    model = os.getenv("MATH_OCR_MODEL", _DEFAULT_MODEL)

    response = await client.messages.create(
        model=model,
        max_tokens=16000,
        # Transcription is a scoped, latency-sensitive task — it needs accuracy,
        # not deliberation. Low effort keeps cost and turnaround down without
        # disabling thinking (which risks internal tags leaking into output).
        output_config={"effort": "low"},
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(image).decode(),
                    },
                },
                {"type": "text", "text": _TRANSCRIBE_PROMPT},
            ],
        }],
    )

    if response.stop_reason == "refusal":
        logger.warning("Vision model declined to transcribe a page")
        return None

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None


async def _transcribe_mathpix(image: bytes) -> Optional[str]:
    """Transcribe a page image with Mathpix, a dedicated formula OCR service."""
    import httpx

    headers = {
        "app_id": os.getenv("MATHPIX_APP_ID", ""),
        "app_key": os.getenv("MATHPIX_APP_KEY", ""),
    }
    payload = {
        "src": "data:image/png;base64," + base64.standard_b64encode(image).decode(),
        "formats": ["text"],
        "math_inline_delimiters": ["$", "$"],
        "math_display_delimiters": ["$$", "$$"],
        "rm_spaces": True,
    }

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            "https://api.mathpix.com/v3/text", headers=headers, json=payload
        )
        response.raise_for_status()
        data = response.json()

    if data.get("error"):
        logger.warning("Mathpix error: %s", data["error"])
        return None
    return (data.get("text") or "").strip() or None


_PROVIDERS = {
    "anthropic": _transcribe_anthropic,
    "mathpix": _transcribe_mathpix,
}


# ── Cache ────────────────────────────────────────────────────────────────────

_memory_cache: Dict[str, str] = {}


def _cache_key(image: bytes, provider: str) -> str:
    return f"mathocr:{provider}:{hashlib.sha256(image).hexdigest()}"


def _cache_get(key: str) -> Optional[str]:
    if key in _memory_cache:
        return _memory_cache[key]
    try:
        import redis

        url = os.getenv("REDIS_URL")
        if not url:
            return None
        value = redis.from_url(url).get(key)
        if value:
            text = value.decode("utf-8")
            _memory_cache[key] = text
            return text
    except Exception:
        # A cache miss must never fail ingestion.
        logger.debug("OCR cache read failed", exc_info=True)
    return None


def _cache_set(key: str, text: str) -> None:
    _memory_cache[key] = text
    try:
        import redis

        url = os.getenv("REDIS_URL")
        if url:
            # Re-ingesting the same document is common while a course is being
            # set up; a month is long enough to make that free.
            redis.from_url(url).setex(key, 30 * 24 * 3600, text)
    except Exception:
        logger.debug("OCR cache write failed", exc_info=True)


# ── Orchestration ────────────────────────────────────────────────────────────


async def _transcribe_page(
    page_number: int, image: bytes, provider: str, semaphore: asyncio.Semaphore
) -> tuple[int, Optional[str]]:
    key = _cache_key(image, provider)
    cached = _cache_get(key)
    if cached is not None:
        return page_number, cached

    transcribe = _PROVIDERS[provider]
    async with semaphore:
        try:
            text = await transcribe(image)
        except Exception:
            logger.warning("OCR failed for page %s", page_number, exc_info=True)
            return page_number, None

    if text:
        _cache_set(key, text)
    return page_number, text


def _blocks_from_transcription(text: str, page_number: int, heading_path: List[str]) -> List[Block]:
    """Re-parse an OCR'd page into blocks, stamped with their real page number."""
    from app.ai.ingestion.math_parser import HEADING, parse_text_blocks

    blocks = parse_text_blocks(text)
    inherited = list(heading_path)

    for block in blocks:
        block["page"] = page_number
        block["needs_math_ocr"] = False
        block["recovered_by_ocr"] = True
        if block["type"] == HEADING:
            inherited = list(block["heading_path"])
        elif not block["heading_path"]:
            # A page that starts mid-section has no heading of its own; keep the
            # section it belongs to so citations and breadcrumbs stay correct.
            block["heading_path"] = inherited

    return blocks


async def apply_math_ocr(blocks: List[Block], content: bytes, filename: str) -> List[Block]:
    """Replace text-layer garble with OCR'd maths on pages that need it.

    Returns the blocks unchanged when the document is not a PDF, when no page
    was flagged, or when no provider is configured.
    """
    if not filename.lower().endswith(".pdf"):
        return blocks

    flagged = sorted({
        b["page"] for b in blocks if b.get("needs_math_ocr") and b.get("page") is not None
    })
    if not flagged:
        return blocks

    provider = _provider_name()
    if provider not in _PROVIDERS:
        logger.warning(
            "%s: %d page(s) contain maths the PDF text layer cannot represent, but "
            "MATH_OCR_PROVIDER is %r — ingesting the degraded text. Formulas on "
            "pages %s will be unreliable.",
            filename, len(flagged), provider, flagged[:10],
        )
        return blocks

    max_pages = int(os.getenv("MATH_OCR_MAX_PAGES", "40"))
    if len(flagged) > max_pages:
        logger.warning(
            "%s: %d pages flagged for OCR, capping at %d", filename, len(flagged), max_pages
        )
        flagged = flagged[:max_pages]

    images = await asyncio.to_thread(_render_pages, content, flagged)
    if not images:
        return blocks

    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    results = await asyncio.gather(*[
        _transcribe_page(page, image, provider, semaphore) for page, image in images.items()
    ])
    transcriptions = {page: text for page, text in results if text}

    if not transcriptions:
        logger.warning("%s: OCR produced nothing; keeping original text", filename)
        return blocks

    # Splice: replace each transcribed page's blocks, keep every other page as-is.
    rebuilt: List[Block] = []
    replaced_pages: set = set()

    for block in blocks:
        page = block.get("page")
        if page in transcriptions:
            if page not in replaced_pages:
                replaced_pages.add(page)
                rebuilt.extend(
                    _blocks_from_transcription(
                        transcriptions[page], page, block.get("heading_path") or []
                    )
                )
            continue  # drop the garbled original
        rebuilt.append(block)

    for index, block in enumerate(rebuilt):
        block["index"] = index

    logger.info(
        "%s: recovered maths on %d/%d page(s) via %s OCR",
        filename, len(transcriptions), len(flagged), provider,
    )
    return rebuilt
