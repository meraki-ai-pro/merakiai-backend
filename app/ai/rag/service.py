import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Union

from app.core import events
from . import crag
from .retriever import retrieve
from .prompt_builder import build_system_and_user
from .claude import generate_response, stream_response

logger = logging.getLogger(__name__)

# Wall-clock ceiling on everything between the student's question and the first
# token: retrieval, re-ranking and any corrective retry. Beyond this the answer
# is built from whatever retrieval has already produced.
RETRIEVAL_DEADLINE = float(os.getenv("RAG_RETRIEVAL_DEADLINE_MS", "5000")) / 1000


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
    academic_level: Optional[str] = None,
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

    # One deadline for the whole retrieval phase, not a budget per stage.
    #
    # Per-stage budgets compound: a turn that hit both the re-rank ceiling and
    # the CRAG ceiling waited 2.5s + 2.0s and then discarded BOTH results,
    # producing an 8.6s time-to-first-token from caps that were each meant to
    # protect it. What a student experiences is the total, so that is what is
    # bounded here.
    deadline = time.monotonic() + RETRIEVAL_DEADLINE

    chunks = await retrieve(
        query=user_message,
        mode=mode,
        course_id=course_id,
        top_k=top_k,
    )

    # Corrective RAG. One rewrite-and-retry when the first pass looks weak;
    # the better of the two results is kept, so a rewrite can never make the
    # answer worse than not rewriting.
    #
    # Budgeted, for the same reason re-ranking is: the retry is a second model
    # call plus a second full retrieval, and it measured as the largest single
    # contributor to slow turns (9.3s against a 3.3s best). A better ordering
    # is not worth doubling the wait — if it cannot be had quickly, the first
    # retrieval is already an answer.
    verdict = crag.assess(chunks)
    remaining = deadline - time.monotonic()

    # Below this there is not enough time left to rewrite AND re-retrieve, so
    # starting would only guarantee a timeout the student pays for.
    can_retry = remaining >= crag.MIN_RETRY_WINDOW

    if crag._enabled() and verdict.should_retry and can_retry:
        if on_progress is not None:
            on_progress("rewriting", "Rephrasing to search the notes again")

        async def _rewrite_and_retry():
            rewritten = await crag.rewrite_query(user_message, course_domain_topics)
            if rewritten == user_message:
                return None
            retried = await retrieve(
                query=rewritten, mode=mode, course_id=course_id, top_k=top_k
            )
            return retried, crag.assess(retried)

        try:
            outcome = await asyncio.wait_for(
                _rewrite_and_retry(),
                # Whichever is smaller: the retry's own budget, or what is
                # actually left of the turn's deadline.
                timeout=min(crag.RETRY_BUDGET, remaining),
            )
        except asyncio.TimeoutError:
            logger.info(
                "CRAG retry exceeded its %.1fs budget; keeping the first retrieval",
                crag.RETRY_BUDGET,
            )
            outcome = None

        if outcome:
            retry, retry_verdict = outcome
            if retry_verdict.best_score > verdict.best_score:
                gain = round(retry_verdict.best_score - verdict.best_score, 4)
                chunks, verdict = retry, retry_verdict
                events.emit(
                    events.QUERY_REWRITTEN,
                    course_id=course_id,
                    payload={"gain": gain, "verdict": retry_verdict.verdict},
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

    if verdict.should_admit_failure and chunks:
        # Distinct from retrieval.empty: something came back, it just was not
        # relevant. That is the harder failure to notice without this event.
        events.emit(
            events.RETRIEVAL_WEAK,
            course_id=course_id,
            payload={"best": verdict.best_score, "usable": verdict.usable},
        )

    # Only when the board is in play — a text or video answer has nowhere to
    # put a video slide, so the lookup would be wasted work.
    video_concepts: List[str] = []
    if board:
        from app.media.render.service import approved_concept_keys

        video_concepts = await asyncio.to_thread(approved_concept_keys, course_id)

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
        video_concepts=video_concepts,
        academic_level=academic_level,
        insufficient_context=crag._enabled() and verdict.should_admit_failure,
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
