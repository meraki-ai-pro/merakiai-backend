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

    options: Dict[str, Any] = {"api_key": api_key}
    workspace_id = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
    if workspace_id:
        options["default_headers"] = {"anthropic-workspace-id": workspace_id}
    return options
