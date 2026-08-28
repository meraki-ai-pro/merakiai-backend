"""The environment a render subprocess is allowed to see.

Shared by both renderers, and shared deliberately. Two properties have to hold
at once, and getting either wrong is quiet rather than loud:

  **Nothing sensitive gets through.** The parent process holds
  ANTHROPIC_API_KEY and the Supabase service-role key. The Manim subprocess
  executes model-generated Python, so anything readable from `os.environ` there
  is a credential that code can exfiltrate. The env is therefore built up from
  an allowlist, never filtered down from the parent's.

  **Enough gets through that the tool actually starts.** This is the half that
  keeps being rediscovered. On Windows a genuinely minimal env does not work:
  CPython cannot initialise without SYSTEMROOT, and Node cannot be launched
  without PATHEXT/COMSPEC because `npx` is really `npx.cmd`. Both failures look
  like the renderer being broken — "python: fatal error", "npx not found" —
  rather than like a missing environment variable.

Manim hit the second problem, was fixed, and Remotion then hit exactly the same
one the first time it ran outside its Linux container. One implementation, so
the next renderer cannot rediscover it a third time.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Path and platform facts only — no credentials. The Windows entries are not a
# relaxation of the sandbox: without them the interpreter does not start, so
# omitting them hardens nothing.
_WINDOWS_SYSTEM_VARS = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "PATHEXT",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


def render_env(workdir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the subprocess environment for a render.

    ``extra`` is for renderer-specific, non-secret settings (NODE_ENV,
    PYTHONDONTWRITEBYTECODE). It is applied last but cannot be used to smuggle
    a credential in: callers pass literals, and the security test asserts on
    the built environment by value, not on the source.
    """
    import os

    env = {
        "PATH": os.environ.get("PATH", ""),
        # Scratch space points at the throwaway workdir so a render cannot
        # write anywhere that outlives it.
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
    }

    if sys.platform == "win32":
        env.update({
            "TEMP": str(workdir),
            "TMP": str(workdir),
            "USERPROFILE": str(workdir),
        })
        for name in _WINDOWS_SYSTEM_VARS:
            value = os.environ.get(name)
            if value:
                env[name] = value

    if extra:
        env.update(extra)

    return env


def resolve_executable(name: str) -> str:
    """Absolute path to a tool, falling back to the bare name.

    Resolved in the PARENT process, where PATHEXT is intact, and handed to the
    child as an absolute path. On Windows `npx` is `npx.cmd`; a child process
    given the bare name and a trimmed environment cannot work that out and
    fails with a bare FileNotFoundError.
    """
    return shutil.which(name) or name
