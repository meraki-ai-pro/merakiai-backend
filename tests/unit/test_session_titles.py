"""Sidebar session titles: cleaned model output, and a rename always wins."""

import asyncio

from app.ai import session_titles as st
from app.core.llm_config import get_mode_config, VALID_MODES


def test_session_title_model_is_a_valid_configurable_mode():
    assert "session_title" in VALID_MODES
    config = get_mode_config("session_title")
    assert config["model"] == "claude-haiku-4-5-20251001"
    assert config["max_tokens"] == 24


def test_clean_title_strips_wrapping_and_labels():
    assert st.clean_title('"The Chain Rule."') == "The Chain Rule"
    assert st.clean_title("Title: Froth Stability\nextra line") == "Froth Stability"
    assert st.clean_title("") is None and st.clean_title(None) is None
    assert st.clean_title("x" * 200) is None


def _run(monkeypatch, *, untitled, stored=True, reply="Normal Distribution"):
    calls, published = [], []
    monkeypatch.setattr(st, "_untitled", lambda sid: untitled)
    monkeypatch.setattr(st, "_store", lambda sid, title: calls.append(title) or stored)

    async def fake_generate(**kwargs):
        assert kwargs["mode"] == "session_title"
        return reply

    monkeypatch.setattr(st, "generate_response", fake_generate)
    asyncio.run(st.title_session("s1", "what is a z table", "Statistics", lambda sid, e: published.append(e)))
    return calls, published


def test_untitled_session_is_named_and_pushed(monkeypatch):
    calls, published = _run(monkeypatch, untitled=True)
    assert calls == ["Normal Distribution"]
    assert published == [{"type": "session_title", "session_id": "s1", "title": "Normal Distribution"}]


def test_a_student_rename_is_never_overwritten(monkeypatch):
    calls, published = _run(monkeypatch, untitled=False)
    assert calls == [] and published == []
    # Renamed while the model was answering: the conditional write finds nothing.
    calls, published = _run(monkeypatch, untitled=True, stored=False)
    assert published == []


def test_failures_never_raise(monkeypatch):
    monkeypatch.setattr(st, "_untitled", lambda sid: (_ for _ in ()).throw(RuntimeError("no column")))
    asyncio.run(st.title_session("s1", "q", "c", lambda *a: None))
