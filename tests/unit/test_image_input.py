"""Student image uploads — handwritten work, textbook pages, drawn graphs.

Ref: AI_Teaching_System_Project_Proposal §6.3

The property that matters most: format comes from the bytes, never from the
Content-Type header or the filename, both of which the client controls.
"""

import base64

import pytest

from app.ai.rag.claude import _user_content
from app.media.image_input import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_TURN,
    SUPPORTED_MEDIA_TYPES,
    ImageValidationError,
    sniff_media_type,
    validate_image,
    validate_images,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"\x00" * 64


class TestSniffing:
    @pytest.mark.parametrize(
        "data,expected",
        [(JPEG, "image/jpeg"), (PNG, "image/png"), (GIF, "image/gif"), (WEBP, "image/webp")],
    )
    def test_recognises_supported_formats(self, data, expected):
        assert sniff_media_type(data) == expected

    def test_webp_size_field_does_not_defeat_detection(self):
        """WebP is RIFF<4-byte size>WEBP — a plain prefix check cannot see the
        second marker, so the length between them must be skipped."""
        for size in (b"\x00\x00\x00\x00", b"\xff\xff\xff\xff", b"\x24\x08\x00\x00"):
            assert sniff_media_type(b"RIFF" + size + b"WEBP" + b"\x00" * 16) == "image/webp"

    def test_riff_that_is_not_webp_is_rejected(self):
        assert sniff_media_type(b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE") is None

    @pytest.mark.parametrize("data", [b"", b"not an image", b"%PDF-1.7", b"PK\x03\x04"])
    def test_rejects_non_images(self, data):
        assert sniff_media_type(data) is None

    def test_truncated_riff_does_not_crash(self):
        assert sniff_media_type(b"RIFF") is None


class TestHeaderIsNotTrusted:
    def test_a_pdf_named_jpg_is_rejected(self):
        """The filename is client-controlled and must not decide the type."""
        with pytest.raises(ImageValidationError):
            validate_image(b"%PDF-1.7 payload", "solution.jpg")

    def test_declared_type_never_overrides_the_bytes(self):
        """A PNG uploaded as .jpeg is still reported as image/png, so the
        media_type sent to the model always matches the payload."""
        assert validate_image(PNG, "work.jpeg")["media_type"] == "image/png"

    def test_every_returned_type_is_one_the_api_accepts(self):
        for data in (JPEG, PNG, GIF, WEBP):
            assert validate_image(data)["media_type"] in SUPPORTED_MEDIA_TYPES


class TestLimits:
    def test_empty_upload_is_rejected(self):
        with pytest.raises(ImageValidationError):
            validate_image(b"")

    def test_oversized_image_is_rejected(self):
        with pytest.raises(ImageValidationError, match="too large"):
            validate_image(JPEG + b"\x00" * MAX_IMAGE_BYTES)

    def test_cap_leaves_room_for_base64_expansion(self):
        """The API limit is 5 MB *after* base64, which inflates by 4/3."""
        assert MAX_IMAGE_BYTES * 4 / 3 < 5 * 1024 * 1024

    def test_too_many_images_rejected(self):
        batch = [(JPEG, "a.jpg")] * (MAX_IMAGES_PER_TURN + 1)
        with pytest.raises(ImageValidationError, match="at most"):
            validate_images(batch)

    def test_batch_at_the_limit_is_accepted(self):
        batch = [(JPEG, "a.jpg")] * MAX_IMAGES_PER_TURN
        assert len(validate_images(batch)) == MAX_IMAGES_PER_TURN

    def test_one_bad_image_rejects_the_whole_batch(self):
        """Silently dropping a page would give the student feedback on part of
        their working with no indication which part was ignored."""
        with pytest.raises(ImageValidationError):
            validate_images([(JPEG, "p1.jpg"), (b"garbage", "p2.jpg")])

    def test_empty_batch_is_fine(self):
        assert validate_images([]) == []


class TestEncoding:
    def test_data_is_base64_and_round_trips(self):
        out = validate_image(PNG)
        assert base64.b64decode(out["data"]) == PNG

    def test_payload_is_json_serialisable_for_celery(self):
        """Celery's JSON serialiser cannot carry raw bytes."""
        import json

        json.dumps(validate_images([(JPEG, "a.jpg")]))


class TestMessageAssembly:
    def test_text_only_stays_a_plain_string(self):
        """The common path must produce byte-identical requests to before."""
        assert _user_content("what is a limit?") == "what is a limit?"
        assert _user_content("hi", []) == "hi"

    def test_images_precede_the_question(self):
        blocks = _user_content("check my working", validate_images([(JPEG, "a.jpg")]))
        assert [b["type"] for b in blocks] == ["image", "text"]

    def test_all_images_are_carried(self):
        imgs = validate_images([(JPEG, "a.jpg"), (PNG, "b.png")])
        blocks = _user_content("q", imgs)
        assert sum(1 for b in blocks if b["type"] == "image") == 2

    def test_block_shape_matches_the_api(self):
        block = _user_content("q", validate_images([(PNG, "a.png")]))[0]
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "image/png"
        assert block["source"]["data"]
