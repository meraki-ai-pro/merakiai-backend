"""Formula OCR stage.

Maths does not survive a PDF text layer — a typeset integral extracts as
mangled glyph runs, and a scanned page extracts as nothing at all. These tests
cover the routing and splicing around the provider call; they never hit the
network.
"""

import asyncio

import pytest

from app.ai.ingestion import math_ocr
from app.ai.ingestion.math_parser import HEADING, PARAGRAPH


def block(text, *, page=1, needs_ocr=False, heading_path=(), index=0):
    return {
        "type": PARAGRAPH,
        "text": text,
        "level": None,
        "heading_path": list(heading_path),
        "page": page,
        "equations": [],
        "has_math": False,
        "needs_math_ocr": needs_ocr,
        "index": index,
    }


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """Never touch the network or a real cache from these tests."""
    monkeypatch.setattr(math_ocr, "_cache_get", lambda key: None)
    monkeypatch.setattr(math_ocr, "_cache_set", lambda key, text: None)
    monkeypatch.setattr(math_ocr, "_memory_cache", {})


class TestRouting:
    def test_docx_is_untouched(self):
        """Word equations are OMML and convert losslessly — OCR would be waste."""
        blocks = [block("x", needs_ocr=True)]
        assert run(math_ocr.apply_math_ocr(blocks, b"", "notes.docx")) is blocks

    def test_pdf_with_nothing_flagged_is_untouched(self):
        blocks = [block("ordinary prose")]
        assert run(math_ocr.apply_math_ocr(blocks, b"", "notes.pdf")) is blocks

    def test_no_provider_configured_degrades_instead_of_failing(self, monkeypatch):
        monkeypatch.setenv("MATH_OCR_PROVIDER", "none")
        blocks = [block("Z 1 0 x2 dx", needs_ocr=True)]
        assert run(math_ocr.apply_math_ocr(blocks, b"", "notes.pdf")) is blocks

    def test_explicit_provider_overrides_autodetection(self, monkeypatch):
        monkeypatch.setenv("MATH_OCR_PROVIDER", "mathpix")
        assert math_ocr._provider_name() == "mathpix"

    def test_auto_prefers_mathpix_when_credentials_exist(self, monkeypatch):
        monkeypatch.setenv("MATH_OCR_PROVIDER", "auto")
        monkeypatch.setenv("MATHPIX_APP_ID", "id")
        monkeypatch.setenv("MATHPIX_APP_KEY", "key")
        assert math_ocr._provider_name() == "mathpix"

    def test_auto_falls_back_to_anthropic(self, monkeypatch):
        monkeypatch.setenv("MATH_OCR_PROVIDER", "auto")
        monkeypatch.delenv("MATHPIX_APP_ID", raising=False)
        monkeypatch.delenv("MATHPIX_APP_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert math_ocr._provider_name() == "anthropic"

    def test_auto_reports_none_without_any_credentials(self, monkeypatch):
        monkeypatch.setenv("MATH_OCR_PROVIDER", "auto")
        for var in ("MATHPIX_APP_ID", "MATHPIX_APP_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        assert math_ocr._provider_name() == "none"


class TestSplicing:
    """The OCR'd page replaces its garbled originals; other pages are kept."""

    @pytest.fixture
    def spliced(self, monkeypatch):
        monkeypatch.setenv("MATH_OCR_PROVIDER", "anthropic")
        monkeypatch.setattr(
            math_ocr, "_render_pages", lambda content, pages: {p: b"png" for p in pages}
        )

        async def fake(image):
            return "## Integrals\n\nWe have $\\int_0^1 x^2 dx = \\frac{1}{3}$ exactly."

        monkeypatch.setitem(math_ocr._PROVIDERS, "anthropic", fake)

        blocks = [
            block("Clean prose on page 1.", page=1, index=0),
            block("Z 1 0 x2dx garble", page=2, needs_ocr=True, index=1),
            block("more garble", page=2, needs_ocr=True, index=2),
            block("Clean prose on page 3.", page=3, index=3),
        ]
        return run(math_ocr.apply_math_ocr(blocks, b"%PDF", "notes.pdf"))

    def test_garbled_blocks_are_removed(self, spliced):
        assert not any("garble" in b["text"] for b in spliced)

    def test_recovered_latex_is_present(self, spliced):
        assert any(r"\int_0^1" in b["text"] for b in spliced)

    def test_untouched_pages_survive(self, spliced):
        texts = [b["text"] for b in spliced]
        assert "Clean prose on page 1." in texts
        assert "Clean prose on page 3." in texts

    def test_recovered_blocks_keep_their_page_number(self, spliced):
        recovered = [b for b in spliced if b.get("recovered_by_ocr")]
        assert recovered
        assert all(b["page"] == 2 for b in recovered)

    def test_recovered_blocks_clear_the_ocr_flag(self, spliced):
        """Otherwise a re-ingest would OCR the same page forever."""
        assert all(not b.get("needs_math_ocr") for b in spliced)

    def test_indexes_are_resequenced(self, spliced):
        assert [b["index"] for b in spliced] == list(range(len(spliced)))

    def test_page_order_is_preserved(self, spliced):
        assert [b["page"] for b in spliced] == sorted(b["page"] for b in spliced)


class TestFailureHandling:
    @pytest.fixture(autouse=True)
    def provider(self, monkeypatch):
        monkeypatch.setenv("MATH_OCR_PROVIDER", "anthropic")
        monkeypatch.setattr(
            math_ocr, "_render_pages", lambda content, pages: {p: b"png" for p in pages}
        )

    def test_provider_exception_keeps_the_original_text(self, monkeypatch):
        async def boom(image):
            raise RuntimeError("upstream down")

        monkeypatch.setitem(math_ocr._PROVIDERS, "anthropic", boom)
        blocks = [block("degraded but present", page=1, needs_ocr=True)]
        out = run(math_ocr.apply_math_ocr(blocks, b"%PDF", "notes.pdf"))
        assert out[0]["text"] == "degraded but present"

    def test_empty_transcription_keeps_the_original_text(self, monkeypatch):
        async def empty(image):
            return None

        monkeypatch.setitem(math_ocr._PROVIDERS, "anthropic", empty)
        blocks = [block("degraded but present", page=1, needs_ocr=True)]
        out = run(math_ocr.apply_math_ocr(blocks, b"%PDF", "notes.pdf"))
        assert out[0]["text"] == "degraded but present"

    def test_page_cap_is_respected(self, monkeypatch):
        monkeypatch.setenv("MATH_OCR_MAX_PAGES", "2")
        rendered = {}

        def record(content, pages):
            rendered["pages"] = list(pages)
            return {}

        monkeypatch.setattr(math_ocr, "_render_pages", record)
        blocks = [block("g", page=p, needs_ocr=True, index=p) for p in range(1, 11)]
        run(math_ocr.apply_math_ocr(blocks, b"%PDF", "notes.pdf"))
        assert rendered["pages"] == [1, 2]


class TestTranscriptionParsing:
    def test_headings_become_blocks_and_set_the_path(self):
        blocks = math_ocr._blocks_from_transcription(
            "# Integrals\n\nThe integral $\\int x\\,dx$ is basic.", 7, []
        )
        assert blocks[0]["type"] == HEADING
        assert blocks[1]["heading_path"] == ["Integrals"]
        assert blocks[1]["page"] == 7

    def test_page_starting_mid_section_inherits_the_heading(self):
        """A page break does not restart the section it sits inside."""
        blocks = math_ocr._blocks_from_transcription(
            "Continuing the proof from the previous page.", 8, ["Calculus", "Limits"]
        )
        assert blocks[0]["heading_path"] == ["Calculus", "Limits"]

    def test_latex_is_detected_as_maths(self):
        blocks = math_ocr._blocks_from_transcription("$$\\frac{1}{3}$$", 2, [])
        assert blocks[0]["has_math"] is True
