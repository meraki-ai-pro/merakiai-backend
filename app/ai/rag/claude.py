# app/ai/rag/claude.py
from __future__ import annotations

import os
import asyncio
import random
from typing import Any, Dict, List, Optional

from anthropic import Anthropic
from anthropic._exceptions import OverloadedError, APIError, RateLimitError, APITimeoutError

from app.core.llm_config import get_mode_config  # noqa: F401 — re-exported for callers

api_key = os.getenv("ANTHROPIC_API_KEY")
if not api_key:
    raise RuntimeError("Missing ANTHROPIC_API_KEY environment variable.")

client = Anthropic(api_key=api_key)


async def _call_anthropic(
    prompt: str,
    mode: str,
    system_parts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Run blocking SDK call in a worker thread.

    system_parts, when provided, is passed as the ``system`` parameter and
    may include ``cache_control`` entries for prompt caching.
    """
    config = get_mode_config(mode)

    def _blocking():
        kwargs: Dict[str, Any] = {
            "model": config["model"],
            "max_tokens": config["max_tokens"],
            "messages": [{"role": "user", "content": prompt}],
        }
        if "temperature" in config:
            kwargs["temperature"] = config["temperature"]
        if system_parts:
            kwargs["system"] = system_parts
        if "output_config" in config:
            kwargs["output_config"] = config["output_config"]
        resp = client.messages.create(**kwargs)
        return resp.content[0].text

    return await asyncio.to_thread(_blocking)


async def generate_response(
    prompt: str,
    mode: str,
    system_parts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Safe Claude call with retries for transient outages/overload.

    Pass ``system_parts`` to use a separate system prompt (enables prompt
    caching via ``cache_control`` on stable prefixes).
    """
    max_attempts = int(os.getenv("ANTHROPIC_MAX_RETRIES", "4"))
    base_delay = float(os.getenv("ANTHROPIC_RETRY_BASE_DELAY", "0.8"))  # seconds

    last_err: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await _call_anthropic(prompt, mode, system_parts=system_parts)
        except (OverloadedError, RateLimitError, APITimeoutError, APIError) as e:
            last_err = e

            sleep_s = base_delay * (2 ** (attempt - 1))
            sleep_s += random.uniform(0, 0.25)

            if attempt == max_attempts:
                raise

            await asyncio.sleep(sleep_s)

    raise last_err or RuntimeError("Anthropic call failed")