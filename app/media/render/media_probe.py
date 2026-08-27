"""Reading facts back off a rendered file.

Lives under render/ rather than next to the narration code because the render
containers need it and must not import that module — it pulls the TTS stack,
which those images deliberately do not carry. Both render images install
ffmpeg (render.Dockerfile, remotion.Dockerfile), so ffprobe is available in
every process that imports this.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 30


def binary(name: str) -> str:
    """Absolute path to an ffmpeg-family tool, or the bare name.

    Resolved rather than assumed so a missing ffprobe fails as "not installed"
    instead of a FileNotFoundError from inside subprocess.
    """
    return shutil.which(name) or name


def probe_duration(path: Path | str) -> float | None:
    """Length of a media file in seconds. None when it cannot be determined.

    Never raises. A duration is a display detail — a video whose length we
    cannot read is still a perfectly good video, and failing the render over it
    would trade something valuable for something cosmetic.
    """
    try:
        proc = subprocess.run(
            [
                binary("ffprobe"), "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffprobe failed for %s: %s", path, exc)
        return None

    try:
        return round(float(proc.stdout.strip()), 2)
    except (TypeError, ValueError):
        return None
