# app/ai/rag/claude.py
from __future__ import annotations

import os
import asyncio
import random
from typing import Any, Callable, Dict, List, Optional

from anthropic import Anthropic, AsyncAnthropic
from anthropic._exceptions import OverloadedError, APIError, RateLimitError, APITimeoutError

from app.core.llm_config import get_mode_config  # noqa: F401 — re-exported for callers

# Lazy singletons. These were built at *import* time, which turned a missing
# key into an import-time crash and made every module that transitively imports
# the RAG service unimportable without a full environment. Same pattern already
# applied in pinecone.py, retriever.py and embedder.py.
_client: Anthropic | None = None
_async_client: AsyncAnthropic | None = None


def _get_api_key() -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY environment variable.")
    return api_key


def get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=_get_api_key())
    return _client


def get_async_client() -> AsyncAnthropic:
    global _async_client
    if _async_client is None:
        _async_client = AsyncAnthropic(api_key=_get_api_key())
    return _async_client


def _user_content(
    prompt: str, images: Optional[List[Dict[str, str]]] = None
) -> Any:
    """Build the user message, with any images ahead of the text.

    ``images`` is a list of ``{"media_type": ..., "data": <base64>}``.

    Images go first deliberately: Anthropic's guidance is that a question asked
    *after* the image it refers to is answered more accurately, which matters
    here because the question is usually "what did I get wrong?" about the
    photograph immediately above it.

    Returns the plain string when there are no images, so the overwhelmingly
    common text-only path produces exactly the request it did before.
    """
    if not images:
        return prompt

    blocks: List[Dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["data"],
            },
        }
        for img in images
    ]
    blocks.append({"type": "text", "text": prompt})
    return blocks


async def _call_anthropic(
    prompt: str,
    mode: str,
    system_parts: Optional[List[Dict[str, Any]]] = None,
    images: Optional[List[Dict[str, str]]] = None,
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
            "messages": [{"role": "user", "content": _user_content(prompt, images)}],
        }
        if "temperature" in config:
            kwargs["temperature"] = config["temperature"]
        if system_parts:
            kwargs["system"] = system_parts
        if "output_config" in config:
            kwargs["output_config"] = config["output_config"]
        resp = get_client().messages.create(**kwargs)
        return resp.content[0].text

    return await asyncio.to_thread(_blocking)


async def generate_response(
    prompt: str,
    mode: str,
    system_parts: Optional[List[Dict[str, Any]]] = None,
    images: Optional[List[Dict[str, str]]] = None,
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
            return await _call_anthropic(
                prompt, mode, system_parts=system_parts, images=images
            )
        except (OverloadedError, RateLimitError, APITimeoutError, APIError) as e:
            last_err = e

            sleep_s = base_delay * (2 ** (attempt - 1))
            sleep_s += random.uniform(0, 0.25)

            if attempt == max_attempts:
                raise

            await asyncio.sleep(sleep_s)

    raise last_err or RuntimeError("Anthropic call failed")


async def stream_response(
    prompt: str,
    mode: str,
    system_parts: Optional[List[Dict[str, Any]]] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
    images: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Stream Claude response token-by-token, calling on_chunk(text) for each delta.

    on_chunk is a synchronous callable — safe to call from inside the async loop
    since Redis PUBLISH completes in ~1 ms.

    Falls back to non-streaming generate_response() on any error so the caller
    always gets a complete response string, regardless of streaming failures.
    """
    config = get_mode_config(mode)
    kwargs: Dict[str, Any] = {
        "model": config["model"],
        "max_tokens": config["max_tokens"],
        "messages": [{"role": "user", "content": _user_content(prompt, images)}],
    }
    if "temperature" in config:
        kwargs["temperature"] = config["temperature"]
    if system_parts:
        kwargs["system"] = system_parts
    if "output_config" in config:
        kwargs["output_config"] = config["output_config"]

    try:
        async with get_async_client().messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if on_chunk:
                    on_chunk(text)
            return await stream.get_final_text()
    except Exception:
        # Streaming failed (network error, model overload, etc.).
        # Fall back to the non-streaming path which retries automatically.
        return await generate_response(
            prompt, mode, system_parts=system_parts, images=images
        )