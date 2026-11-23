"""F-08: narration is bound to text the tutor produced, and the server decides how it sounds."""
from app.api.v1.narration import source_is_tutor_text
from app.media.spoken import chunk_for_speech, spoken_answer, to_spoken_text

TUTOR = ["::: slide The Power Rule\nThe derivative of $x^2$ is $2x$.\nBring the power down.\n:::"]


def test_genuine_slide_is_accepted():
    assert source_is_tutor_text("The Power Rule\nThe derivative of $x^2$ is $2x$.\nBring the power down.", TUTOR)


def test_invented_or_empty_source_is_rejected():
    assert not source_is_tutor_text("The lecturer admits the exam is leaked.", TUTOR)
    assert not source_is_tutor_text("anything", [])


def test_reordered_fragments_are_rejected():
    # Each line exists, but not in this order: reassembly must fail.
    assert not source_is_tutor_text("Bring the power down.\nThe Power Rule", TUTOR)


def test_whitespace_differences_are_tolerated():
    assert source_is_tutor_text("Bring   the power\ndown.", TUTOR)


def test_spoken_text_matches_client_conversion():
    assert to_spoken_text("The derivative of $x^2$ is $2x$.") == "The derivative of x squared is 2x."
    assert to_spoken_text("See [1] and **bold**") == "See and bold"
    assert "over" in to_spoken_text("$" + chr(92) + "frac{a}{b}$")


def test_board_fences_are_not_spoken():
    assert spoken_answer("::: plot\n{\"f\":1}\n:::\nHello there.") == "Hello there."


def test_chunking_splits_at_sentence_ends():
    chunks = chunk_for_speech("One. " * 400, 1150)
    assert len(chunks) >= 2 and all(len(c) <= 1150 for c in chunks)
