from typing import Any, Callable, Dict, List, Optional, Union

from .retriever import retrieve
from .prompt_builder import build_system_and_user
from .claude import generate_response, stream_response


async def query_rag(
    user_message: str,
    mode: str,
    course_id: str,
    course_persona: str = None,
    course_domain_topics: List[str] = None,
    memory: List[str] = None,
    top_k: int = 6,
    on_chunk: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[str, str], None]] = None,
    on_sources: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    concise: bool = False,
    board: bool = False,
    images: Optional[List[Dict[str, str]]] = None,
) -> Union[Dict[str, Any], Dict[str, str]]:
    """Full RAG pipeline for a single conversational turn.

    1. Retrieve context (hybrid dense + lexical, fused, optionally re-ranked)
    2. Split prompt into stable system text and dynamic user text
    3. Call Claude with cache_control on the system prefix (prompt caching)
    4. Return the answer together with the sources it was grounded in

    ``on_progress(stage, label)`` is an optional synchronous callback invoked
    at each phase boundary so the UI can show "what's happening" (retrieving
    course context, then writing the answer). It is safe to call from the async
    loop — publishing a progress event completes in ~1 ms.

    ``on_sources(sources)`` is invoked once retrieval settles, before generation
    starts, so the client can render what the answer is being built from while
    it is still being written.

    ``images`` carries validated base64 blocks for multimodal turns (a student
    photographing handwritten work). Note that retrieval still runs on
    ``user_message`` alone — the image is not searchable — so a turn with a
    photo and no question retrieves on the caller-supplied fallback prompt.
    """
    if on_progress is not None:
        on_progress("retrieving", "Searching course materials")

    chunks = await retrieve(
        query=user_message,
        mode=mode,
        course_id=course_id,
        top_k=top_k,
    )

    # The model reads the parent section of each match (small-to-big); the
    # source list carries the provenance for the citation markers it will emit.
    context = [chunk.context_text for chunk in chunks]
    sources = [chunk.to_source() for chunk in chunks]

    # Only offer citations when the retrieved material can actually be
    # identified. Legacy-namespace chunks have no filename or page, so asking
    # for citations there would invite invented ones.
    citable = [s for s in sources if s.get("source_filename") or s.get("section_title")]
    cite_sources = sources if len(citable) == len(sources) and sources else None

    if on_sources is not None and sources:
        on_sources(sources)

    system_text, user_text = build_system_and_user(
        user_message=user_message,
        context=context,
        mode=mode,
        memory=memory,
        course_persona=course_persona,
        course_domain_topics=course_domain_topics,
        concise=concise,
        board=board,
        sources=cite_sources,
    )

    if on_progress is not None:
        on_progress("generating", "Writing your answer")

    # cache_control marks the system prefix as cacheable for 5 minutes.
    # Subsequent turns in the same session on the same course reuse the cache,
    # reducing input token cost by ~90% on the cached portion.
    system_parts = [
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    if on_chunk is not None:
        raw_output = await stream_response(
            prompt=user_text, mode=mode, system_parts=system_parts,
            on_chunk=on_chunk, images=images,
        )
    else:
        raw_output = await generate_response(
            prompt=user_text, mode=mode, system_parts=system_parts, images=images,
        )

    return {
        "mode": mode,
        "response": raw_output.strip(),
        "sources": sources,
    }
