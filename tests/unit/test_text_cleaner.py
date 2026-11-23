"""Text sent to a voice must not narrate citation UI markers."""

from app.media.text_cleaner import clean_for_tts, strip_citations


def test_strips_single_and_adjacent_citation_markers():
    text = "A limit describes nearby behaviour [1]. Both sources agree [2][3]."

    assert clean_for_tts(text) == (
        "A limit describes nearby behaviour. Both sources agree."
    )


def test_keeps_visible_answer_unchanged_by_returning_new_text():
    answer = "The derivative is a rate of change [1]."

    spoken = strip_citations(answer)

    assert answer == "The derivative is a rate of change [1]."
    assert spoken == "The derivative is a rate of change."


def test_does_not_strip_markdown_links_with_numeric_labels():
    assert strip_citations("See [1](https://example.com) for details.") == (
        "See [1](https://example.com) for details."
    )
