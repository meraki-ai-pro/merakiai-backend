from __future__ import annotations

import os
from typing import Any, Dict


def client_options() -> Dict[str, Any]:
    """Return shared Anthropic SDK options without logging credentials.

    Standard Console API keys only need ``api_key``. Identity-linked keys are
    scoped to a workspace and the API rejects them unless the workspace header
    is supplied explicitly.
    """

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY environment variable.")

    # The SDK has its own retry loop by default.  The application already
    # retries transient failures with jitter, so leaving both enabled can turn
    # one slow request into several minutes of invisible nested retries.
    options: Dict[str, Any] = {"api_key": api_key, "max_retries": 0}
    workspace_id = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
    if workspace_id:
        options["default_headers"] = {"anthropic-workspace-id": workspace_id}
    return options
