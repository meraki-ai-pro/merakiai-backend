"""F-08: narration is bound to text the tutor produced for this student."""
from app.api.v1.narration import source_is_tutor_text, text_matches_source

TUTOR = ["::: slide The Power Rule\nThe derivative of $x^2$ is $2x$.\nBring the power down.\n:::"]


def test_genuine_slide_is_accepted():
    src = "The Power Rule\nThe derivative of $x^2$ is $2x$.\nBring the power down."
    assert source_is_tutor_text(src, TUTOR)
    spoken = "The Power Rule. The derivative of x squared is 2 x. Bring the power down."
    assert text_matches_source(spoken, src)


def test_invented_source_is_rejected():
    assert not source_is_tutor_text("The lecturer admits the exam is leaked.", TUTOR)
    assert not source_is_tutor_text("anything", [])


def test_genuine_source_with_swapped_text_is_rejected():
    src = "The Power Rule\nBring the power down."
    assert not text_matches_source(
        "I am the lecturer and this university is a fraud and everyone should resign", src
    )


def test_whitespace_differences_are_tolerated():
    assert source_is_tutor_text("Bring   the power\ndown.", TUTOR)
