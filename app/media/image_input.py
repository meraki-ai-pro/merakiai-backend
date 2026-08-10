"""Validation for student-submitted images (handwritten work, textbook pages).

Ref: AI_Teaching_System_Project_Proposal §6.3 — students photograph handwritten
solutions and receive targeted feedback.

Format is decided by inspecting the bytes, never by trusting the upload's
Content-Type header or filename extension. A mislabelled upload would otherwise
reach the model with a media_type that does not match its content, and the
declared type is attacker-controlled.
"""

from __future__ import annotations

import base64
import logging

logger = logging.getLogger(__name__)

# Anthropic accepts these four. Anything else must be converted client-side.
SUPPORTED_MEDIA_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")

# Anthropic's per-image ceiling is 5 MB *after* base64 expansion, and base64
# inflates by ~4/3. Capping the raw bytes at 3.5 MB keeps the encoded payload
# safely under that without needing to encode first to find out.
MAX_IMAGE_BYTES = int(3.5 * 1024 * 1024)

# Cap per turn. Each image is roughly 1.6k tokens at full size, so the ceiling
# is about cost and latency rather than correctness.
MAX_IMAGES_PER_TURN = 4

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class ImageValidationError(ValueError):
    """Raised when an upload cannot be used as a model input."""


def sniff_media_type(data: bytes) -> str | None:
    """Identify an image from its leading bytes. None if unrecognised."""
    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            return media_type

    # WebP is RIFF-framed: "RIFF" <4-byte size> "WEBP". The size field sits
    # between the two markers, so a plain startswith() check cannot see it.
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    return None


def validate_image(data: bytes, filename: str = "") -> dict[str, str]:
    """Return a model-ready ``{"media_type", "data"}`` block, or raise.

    The returned ``data`` is base64 text, which is what the Anthropic API takes
    and also what survives Celery's JSON serialiser — the task queue cannot
    carry raw bytes.
    """
    if not data:
        raise ImageValidationError("The image file is empty.")

    if len(data) > MAX_IMAGE_BYTES:
        mb = MAX_IMAGE_BYTES / (1024 * 1024)
        raise ImageValidationError(
            f"Image is too large. Please keep photos under {mb:.1f} MB — "
            "most phone cameras let you send a smaller copy."
        )

    media_type = sniff_media_type(data)
    if media_type is None:
        raise ImageValidationError(
            "That file is not a supported image. Please upload a JPEG, PNG, "
            "GIF or WebP photo."
        )

    if filename:
        logger.debug("Accepted image %s as %s (%d bytes)", filename, media_type, len(data))

    return {"media_type": media_type, "data": base64.b64encode(data).decode("ascii")}


def validate_images(files: list[tuple[bytes, str]]) -> list[dict[str, str]]:
    """Validate a batch, rejecting the whole turn if any one fails.

    Partial acceptance would be worse than refusal: the student would get an
    answer about some of their working and have no idea which page was ignored.
    """
    if not files:
        return []

    if len(files) > MAX_IMAGES_PER_TURN:
        raise ImageValidationError(
            f"Please attach at most {MAX_IMAGES_PER_TURN} images at a time."
        )

    return [validate_image(data, filename) for data, filename in files]
