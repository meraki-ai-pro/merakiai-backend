"""Retrieval: hybrid search, fusion, re-ranking, and source attribution.

The previous implementation ran one dense vector search and returned the top
matches as bare strings. Two consequences followed. Exact-token questions
retrieved poorly — a student asking about "Theorem 4.2" or "the chi-squared
statistic" depends on lexical overlap that a dense embedding smooths away. And
because scores and metadata were discarded at this boundary, nothing downstream
could attribute a sentence to a source, which made citations impossible however
much provenance ingestion recorded.

This module keeps the provenance and adds three stages:

1. **Dense retrieval, widened.** Fetch a candidate pool several times larger
   than the answer needs, across both namespace generations (§7.3).
2. **Lexical scoring and fusion.** Score the pool with BM25 and fuse the two
   rankings with Reciprocal Rank Fusion, so a chunk wins by being good on
   either signal rather than only on cosine similarity.
3. **Re-ranking (optional).** A cross-encoder or LLM judge orders the survivors
   by actual relevance to the question.

Retrieval then expands each surviving child chunk to its parent section
(small-to-big, §8.7) and hands back structured, citable chunks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from openai import AsyncOpenAI
from pinecone import Pinecone

from app.ai.ingestion.namespaces import search_namespaces
from app.ai.rag import embedding_cache
from app.ai.rag.visibility import visible_document_ids
from app.core.events import RETRIEVAL_EMPTY, emit
from app.db.supabase import get_async_supabase

logger = logging.getLogger(__name__)

# Lazy singletons
_openai_client: AsyncOpenAI | None = None
_pinecone_index = None

# How many candidates to pull before fusion and re-ranking. Retrieval quality
# depends far more on the pool being wide than on the final k being large.
_CANDIDATE_POOL = int(os.getenv("RAG_CANDIDATES", "30"))


def _get_openai() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing OPENAI_API_KEY environment variable.")
        _openai_client = AsyncOpenAI(api_key=api_key)
    return _openai_client


def _get_pinecone_index():
    global _pinecone_index
    if _pinecone_index is None:
        api_key = os.getenv("PINECONE_API_KEY")
        index_name = os.getenv("PINECONE_INDEX") or os.getenv("PINECONE_INDEX_NAME")
        if not api_key or not index_name:
            raise RuntimeError(
                "Missing PINECONE_API_KEY or PINECONE_INDEX/PINECONE_INDEX_NAME environment variables."
            )
        pc = Pinecone(api_key=api_key)
        _pinecone_index = pc.Index(index_name)
    return _pinecone_index


# ── Retrieved chunk ──────────────────────────────────────────────────────────


@dataclass
class RetrievedChunk:
    """One retrieved passage with everything a citation needs."""

    id: str
    text: str
    score: float
    dense_score: float = 0.0
    lexical_score: float = 0.0
    rerank_score: Optional[float] = None
    parent_text: str = ""
    parent_id: str = ""
    document_id: Optional[str] = None
    source_filename: Optional[str] = None
    section_title: Optional[str] = None
    heading_path: List[str] = field(default_factory=list)
    page: Optional[int] = None
    page_end: Optional[int] = None
    has_math: bool = False
    topic: str = ""
    namespace: str = ""
    # 1-based marker the model cites and the client renders. Assigned last.
    citation: int = 0

    @property
    def context_text(self) -> str:
        """What the model reads: the parent section when there is one.

        Small-to-big — the child was embedded because it matched precisely, but
        the model reasons better over the surrounding section.
        """
        return self.parent_text or self.text

    @property
    def location(self) -> str:
        """Human-readable source label, e.g. 'notes.pdf, p. 7 — Limits'."""
        parts: List[str] = []
        if self.source_filename:
            parts.append(self.source_filename)
        if self.page is not None:
            if self.page_end is not None and self.page_end != self.page:
                parts.append(f"pp. {self.page}-{self.page_end}")
            else:
                parts.append(f"p. {self.page}")
        trail = self.section_title or (self.heading_path[-1] if self.heading_path else "")
        label = ", ".join(parts)
        if trail:
            label = f"{label} — {trail}" if label else trail
        return label or "course material"

    def to_source(self) -> Dict[str, Any]:
        """Serialisable form for the WebSocket payload and the client."""
        return {
            "citation": self.citation,
            "id": self.id,
            "text": self.text,
            "location": self.location,
            "document_id": self.document_id,
            "source_filename": self.source_filename,
            "section_title": self.section_title,
            "heading_path": self.heading_path,
            "page": self.page,
            "page_end": self.page_end,
            "has_math": self.has_math,
            "score": round(self.score, 4),
            "relevance": self.relevance_band,
        }

    @property
    def relevance_band(self) -> str:
        """Coarse confidence for the UI. Deliberately three buckets, not a
        false-precision percentage."""
        basis = self.rerank_score if self.rerank_score is not None else self.dense_score
        if basis >= 0.75:
            return "high"
        if basis >= 0.45:
            return "medium"
        return "low"


# ── Embedding (unchanged cache behaviour) ────────────────────────────────────


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_embedding(embedding):
    if isinstance(embedding, str):
        embedding = json.loads(embedding)
    return [float(v) for v in embedding]


async def _get_cached_embedding(text: str):
    supabase = await get_async_supabase()
    text_hash = _hash_text(text)
    try:
        res = await (
            supabase.table("embedding_cache")
            .select("embedding")
            .eq("text_hash", text_hash)
            .execute()
        )
        if res.data:
            return res.data[0]["embedding"]
    except Exception as e:
        logger.error(f"Failed to fetch from embedding cache: {e}")
    return None


async def _store_embedding(text: str, embedding):
    supabase = await get_async_supabase()
    text_hash = _hash_text(text)
    try:
        await supabase.table("embedding_cache").insert(
            {"text_hash": text_hash, "embedding": embedding}
        ).execute()
    except Exception as e:
        logger.warning(f"Failed to store in embedding cache: {e}")


async def embed_query(query: str) -> List[float]:
    """Embed a student's question, cached in memory then Redis.

    The Supabase-backed cache this replaced was measured at 687ms to read and
    891ms to write, against a ~500ms OpenAI call — it cost more than the work
    it avoided, and the write blocked the turn after the embedding was already
    in hand. See app/ai/rag/embedding_cache.py for the numbers.

    Set EMBED_CACHE_SUPABASE=1 to additionally write through to the old
    embedding_cache table. It is never read on this path.
    """
    cached = await embedding_cache.get(query)
    if cached is not None:
        return _normalize_embedding(cached)

    client = _get_openai()
    response = await client.embeddings.create(
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large"),
        input=query,
    )
    embedding = response.data[0].embedding

    # Not awaited: the answer does not depend on the cache write landing.
    embedding_cache.put_background(query, embedding)
    if os.getenv("EMBED_CACHE_SUPABASE", "0").strip().lower() in ("1", "true", "yes"):
        asyncio.create_task(_store_embedding(query, embedding))

    return _normalize_embedding(embedding)


# ── Lexical scoring (BM25) ───────────────────────────────────────────────────

# Words, identifiers like "chi2", and decimal/section numbers like "4.2" kept
# whole. Splitting "4.2" into "4" and "2" would lose exactly the signal lexical
# search is here to provide.
_TOKEN_RE = re.compile(r"[a-z]+[a-z0-9]*(?:'[a-z]+)?|\d+(?:\.\d+)*")

# Words that carry no discriminative signal in a question about course content.
_STOPWORDS = frozenset("""
a an and are as at be by can could do does for from had has have how i if in into is it
its me my of on or our so than that the their then there these they this to was we were
what when where which who why will with would you your explain tell about please
""".split())


def tokenize(text: str) -> List[str]:
    """Tokenise for lexical scoring, dropping only stopwords.

    Short tokens are deliberately kept: a single letter is meaningful in
    mathematics ("solve for x"), and BM25's inverse document frequency already
    drives near-ubiquitous terms towards zero weight without a length filter
    guessing on its behalf.
    """
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def bm25_scores(query: str, documents: Sequence[str], k1: float = 1.5, b: float = 0.75) -> List[float]:
    """Score documents against the query with Okapi BM25.

    Implemented directly rather than pulled in as a dependency: the corpus here
    is one candidate pool of a few dozen passages, so there is no index to
    build and nothing to persist.
    """
    if not documents:
        return []

    query_terms = tokenize(query)
    if not query_terms:
        return [0.0] * len(documents)

    tokenized = [tokenize(doc) for doc in documents]
    lengths = [len(t) for t in tokenized]
    avg_length = (sum(lengths) / len(lengths)) or 1.0
    total = len(documents)

    frequencies: List[Dict[str, int]] = []
    for tokens in tokenized:
        counts: Dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        frequencies.append(counts)

    scores = [0.0] * total
    for term in set(query_terms):
        containing = sum(1 for counts in frequencies if term in counts)
        if containing == 0:
            continue
        idf = math.log(1 + (total - containing + 0.5) / (containing + 0.5))
        for i, counts in enumerate(frequencies):
            tf = counts.get(term, 0)
            if tf == 0:
                continue
            norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * lengths[i] / avg_length))
            scores[i] += idf * norm

    return scores


def reciprocal_rank_fusion(rankings: Sequence[Sequence[int]], k: int = 60) -> Dict[int, float]:
    """Fuse several rankings of the same items into one score per item.

    RRF is used rather than a weighted sum of raw scores because cosine
    similarity and BM25 are on incomparable scales — normalising them against
    each other would be arbitrary, whereas rank position is directly
    comparable.
    """
    fused: Dict[int, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + position + 1)
    return fused


# ── Dense retrieval ──────────────────────────────────────────────────────────


def _chunk_from_match(match: Dict[str, Any], namespace: str) -> RetrievedChunk:
    """Build a chunk from a Pinecone match.

    Handles both namespace generations: v2 vectors carry full provenance, v1
    vectors carry only ``{mode, topic, text}``. A v1 chunk still retrieves and
    still answers — it just cannot be cited precisely.
    """
    metadata = match.get("metadata") or {}
    page = metadata.get("page")
    page_end = metadata.get("page_end")

    return RetrievedChunk(
        id=str(match.get("id", "")),
        text=metadata.get("text") or metadata.get("content") or "",
        score=0.0,
        dense_score=float(match.get("score") or 0.0),
        parent_text=metadata.get("parent_text") or "",
        parent_id=metadata.get("parent_id") or "",
        document_id=metadata.get("document_id"),
        source_filename=metadata.get("source_filename"),
        section_title=metadata.get("section_title"),
        heading_path=list(metadata.get("heading_path") or []),
        page=int(page) if isinstance(page, (int, float)) else None,
        page_end=int(page_end) if isinstance(page_end, (int, float)) else None,
        has_math=bool(metadata.get("has_math")),
        topic=metadata.get("topic") or "",
        namespace=namespace,
    )


def _build_filter(mode: str, document_ids: List[str] | None) -> Dict[str, Any]:
    """Pinecone metadata filter for one namespace query.

    ``document_ids`` of None means every document in the course is visible, so
    no id clause is added and the query is exactly what it was before this
    feature existed. An *empty list* is different and must be honoured: it means
    every document is unpublished, and the correct result is nothing.
    """
    query_filter: Dict[str, Any] = {"mode": {"$eq": mode}}
    if document_ids is not None:
        query_filter["document_id"] = {"$in": document_ids}
    return query_filter


async def _query_namespace(
    embedding: List[float],
    namespace: str,
    mode: str,
    top_k: int,
    document_ids: List[str] | None = None,
) -> List[RetrievedChunk]:
    index = _get_pinecone_index()
    try:
        results = await asyncio.to_thread(
            index.query,
            vector=embedding,
            top_k=top_k,
            include_metadata=True,
            namespace=namespace,
            filter=_build_filter(mode, document_ids),
        )
    except Exception as e:
        logger.warning("Pinecone query failed for namespace %s: %s", namespace, e)
        return []

    return [
        chunk
        for chunk in (_chunk_from_match(m, namespace) for m in results.get("matches", []))
        if chunk.text
    ]


# ── Re-ranking ───────────────────────────────────────────────────────────────


def _rerank_provider() -> str:
    configured = os.getenv("RAG_RERANK", "auto").strip().lower()
    if configured != "auto":
        return configured
    # Re-ranking is on by default, judged by a small fast model. A dedicated
    # cross-encoder is preferred when one is configured — it is cheaper and
    # roughly an order of magnitude faster — otherwise the LLM judge runs on
    # the Anthropic key the application already holds.
    if os.getenv("COHERE_API_KEY"):
        return "cohere"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "llm"
    return "off"


# Persistent HTTP client for Cohere.
#
# This used to be `async with httpx.AsyncClient(...)` inside the function, which
# meant a fresh TLS handshake to api.cohere.com on EVERY turn — measured at
# roughly a second, against ~200ms of actual reranking.
#
# It is deliberately the SYNC client, called through a thread. Celery runs
# asyncio.run() per task, so every turn gets a brand new event loop; an
# AsyncClient binds its connection pool to the loop that created it and would
# be unusable (or silently re-handshake) on the next turn. A sync client is not
# loop-bound, so the connection genuinely survives.
_cohere_http = None


def _get_cohere_client():
    global _cohere_http
    if _cohere_http is None:
        import httpx

        _cohere_http = httpx.Client(
            # Below the rerank budget, so httpx gives up first and we get a
            # clean error rather than a cancelled task mid-flight.
            timeout=float(os.getenv("RAG_RERANK_BUDGET_MS", "2500")) / 1000,
            # Keep the connection alive well past the gap between turns.
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300),
            headers={"Authorization": f"Bearer {os.getenv('COHERE_API_KEY')}"},
        )
    return _cohere_http


async def _rerank_cohere(query: str, chunks: List[RetrievedChunk], top_k: int) -> List[RetrievedChunk]:
    model = os.getenv("RAG_RERANK_MODEL_COHERE", "rerank-v3.5")

    def _post():
        response = _get_cohere_client().post(
            "https://api.cohere.com/v2/rerank",
            json={
                "model": model,
                # Truncated: the reranker judges relevance, and sending whole
                # parent sections inflates the request without improving the
                # ordering. 600 chars matches the LLM judge's window.
                "documents": [c.text[:600] for c in chunks],
                "query": query,
                "top_n": min(top_k, len(chunks)),
            },
        )
        response.raise_for_status()
        return response.json()

    payload = await asyncio.to_thread(_post)

    ordered: List[RetrievedChunk] = []
    for result in payload.get("results", []):
        chunk = chunks[result["index"]]
        chunk.rerank_score = float(result.get("relevance_score") or 0.0)
        ordered.append(chunk)
    return ordered


_RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "order": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Passage numbers, most relevant first.",
        }
    },
    "required": ["order"],
    "additionalProperties": False,
}


async def _rerank_llm(query: str, chunks: List[RetrievedChunk], top_k: int) -> List[RetrievedChunk]:
    """Order passages with a small, fast model acting as a relevance judge."""
    # Reuses the shared client rather than constructing an AsyncAnthropic per
    # call — same TLS-handshake-per-turn problem as the Cohere path had.
    from app.ai.rag.claude import get_client

    client = get_client()
    # A reranker runs inside the latency budget of every turn and does no
    # generation, so it is deliberately a small model. Override if needed.
    model = os.getenv("RAG_RERANK_MODEL", "claude-haiku-4-5")

    listing = "\n\n".join(
        f"[{i}] {c.text[:600]}" for i, c in enumerate(chunks)
    )
    prompt = (
        "Order these course-material passages by how well each one helps answer "
        "the student's question. Put the most useful first and omit any that are "
        "irrelevant.\n\n"
        f"STUDENT QUESTION:\n{query}\n\nPASSAGES:\n{listing}"
    )

    # Sync client on a thread, for the same loop-binding reason as Cohere.
    response = await asyncio.to_thread(
        lambda: client.messages.create(
            model=model,
            max_tokens=500,
            output_config={"format": {"type": "json_schema", "schema": _RERANK_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    )

    text = "".join(b.text for b in response.content if b.type == "text")
    order = json.loads(text).get("order", [])

    ordered: List[RetrievedChunk] = []
    seen: set = set()
    for position, raw_index in enumerate(order):
        if not isinstance(raw_index, int) or raw_index in seen:
            continue
        if 0 <= raw_index < len(chunks):
            seen.add(raw_index)
            chunk = chunks[raw_index]
            # Positional score, so the relevance band still means something.
            chunk.rerank_score = max(0.0, 1.0 - position / max(len(chunks), 1))
            ordered.append(chunk)
        if len(ordered) >= top_k:
            break
    return ordered


# Hard ceiling on reranking, measured against what it buys.
#
# Reranking improves precision by roughly a second's worth of work. It is never
# worth twenty. The old code inherited a 20s HTTP timeout, so a throttled or
# stalled reranker turned a 4s turn into a 24s one before falling back — and it
# DID fall back, so the student waited twenty seconds for nothing.
_RERANK_BUDGET = float(os.getenv("RAG_RERANK_BUDGET_MS", "2500")) / 1000

# Circuit breaker. Once a provider is failing — a rate-limited free-tier key,
# an outage — paying the budget on every single turn is pure loss, because the
# fallback is already known to be acceptable. Trip open, retry occasionally.
_RERANK_FAILS = 0
_RERANK_OPEN_UNTIL = 0.0
_RERANK_TRIP_AFTER = int(os.getenv("RAG_RERANK_TRIP_AFTER", "3"))
_RERANK_COOLDOWN = float(os.getenv("RAG_RERANK_COOLDOWN_S", "60"))


def _rerank_circuit_open() -> bool:
    return time.monotonic() < _RERANK_OPEN_UNTIL


def _note_rerank_failure() -> None:
    global _RERANK_FAILS, _RERANK_OPEN_UNTIL
    _RERANK_FAILS += 1
    if _RERANK_FAILS >= _RERANK_TRIP_AFTER:
        _RERANK_OPEN_UNTIL = time.monotonic() + _RERANK_COOLDOWN
        _RERANK_FAILS = 0
        logger.warning(
            "Re-ranking disabled for %.0fs after %d consecutive failures; "
            "answers continue on the fused order",
            _RERANK_COOLDOWN, _RERANK_TRIP_AFTER,
        )


def _note_rerank_success() -> None:
    global _RERANK_FAILS
    _RERANK_FAILS = 0


async def _maybe_rerank(query: str, chunks: List[RetrievedChunk], top_k: int) -> List[RetrievedChunk]:
    provider = _rerank_provider()
    if provider == "off" or len(chunks) <= 1:
        return chunks[:top_k]

    if _rerank_circuit_open():
        return chunks[:top_k]

    try:
        if provider == "cohere":
            coro = _rerank_cohere(query, chunks, top_k)
        elif provider == "llm":
            coro = _rerank_llm(query, chunks, top_k)
        else:
            logger.warning("Unknown RAG_RERANK provider %r — skipping", provider)
            return chunks[:top_k]

        ordered = await asyncio.wait_for(coro, timeout=_RERANK_BUDGET)
    except asyncio.TimeoutError:
        _note_rerank_failure()
        logger.warning(
            "Re-ranking exceeded its %.1fs budget; using the fused order", _RERANK_BUDGET
        )
        return chunks[:top_k]
    except Exception:
        # Re-ranking is a precision improvement, never a dependency. Fused
        # order is already a good answer.
        _note_rerank_failure()
        logger.warning("Re-ranking failed; falling back to fused order", exc_info=True)
        return chunks[:top_k]

    _note_rerank_success()
    return ordered[:top_k] if ordered else chunks[:top_k]


# ── Public API ───────────────────────────────────────────────────────────────


def _dedupe_by_parent(chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
    """Keep the best child per parent section.

    Several children of one section frequently match the same question. Sending
    the section three times crowds out other material and wastes context.
    """
    best: List[RetrievedChunk] = []
    seen_parents: set = set()
    for chunk in chunks:
        key = chunk.parent_id or chunk.id
        if key in seen_parents:
            continue
        seen_parents.add(key)
        best.append(chunk)
    return best


async def retrieve(
    query: str,
    mode: str,
    course_id: str,
    top_k: int = 6,
    candidates: int | None = None,
) -> List[RetrievedChunk]:
    """Hybrid retrieval with fusion, optional re-ranking, and attribution."""
    mode = (mode or "learn").lower().strip()
    course_id = (course_id or "").strip()
    if not course_id:
        raise ValueError("course_id is required for retrieval")

    pool_size = candidates or _CANDIDATE_POOL
    namespaces = search_namespaces(course_id, mode)
    logger.info("Retrieving  course=%s  mode=%s  namespaces=%s", course_id, mode, namespaces)

    # Permission stack step 6. Runs concurrently with embedding rather than
    # before it — both are needed before the first Pinecone call, so serialising
    # them would add the DB round trip to time-to-first-token for nothing.
    embedding, document_ids = await asyncio.gather(
        embed_query(query),
        visible_document_ids(course_id, mode),
    )

    if document_ids is not None and not document_ids:
        logger.info(
            "No published documents for course=%s mode=%s — retrieval skipped",
            course_id, mode,
        )
        return []

    # Both namespace generations are searched concurrently, so the fallback
    # costs no extra wall-clock time.
    per_namespace = await asyncio.gather(
        *[
            _query_namespace(embedding, ns, mode, pool_size, document_ids)
            for ns in namespaces
        ]
    )

    # search_namespaces() orders the current generation first. Take the first
    # generation that returns anything and ignore the rest — the legacy
    # namespace is a fallback for courses not yet re-ingested, not a second
    # source to merge with. Merging them would put two chunkings of the same
    # passage in one pool, and because legacy vectors carry no provenance it
    # would also suppress citations (§7.3.3) for a course that has in fact been
    # re-ingested.
    chunks_for_generation = next((chunks for chunks in per_namespace if chunks), [])

    pool: List[RetrievedChunk] = []
    seen_ids: set = set()
    for chunk in chunks_for_generation:
        if chunk.id in seen_ids:
            continue
        seen_ids.add(chunk.id)
        pool.append(chunk)

    if not pool:
        logger.info("No chunks retrieved for course=%s mode=%s", course_id, mode)
        # A research metric in its own right: how often the knowledge base
        # fails to answer, which is what CRAG (#24) would act on.
        emit(RETRIEVAL_EMPTY, course_id=course_id, payload={"mode": mode})
        return []

    # Dense ranking (already sorted per namespace; re-sort across the merged pool).
    dense_rank = sorted(range(len(pool)), key=lambda i: pool[i].dense_score, reverse=True)

    # Lexical ranking over the same pool. Only passages BM25 actually matched
    # take part: RRF scores by rank position, not by score, so including the
    # zero-scoring majority would hand nearly full lexical credit to passages
    # with no term overlap at all and drown out the one real match.
    lexical = bm25_scores(query, [c.text for c in pool])
    for i, score in enumerate(lexical):
        pool[i].lexical_score = score
    lexical_rank = sorted(
        (i for i in range(len(pool)) if lexical[i] > 0),
        key=lambda i: lexical[i],
        reverse=True,
    )

    fused = reciprocal_rank_fusion([dense_rank, lexical_rank])
    for index, score in fused.items():
        pool[index].score = score

    ranked = sorted(pool, key=lambda c: c.score, reverse=True)
    ranked = _dedupe_by_parent(ranked)

    # Re-rank a shortlist rather than the whole pool — cost scales with input.
    shortlist = ranked[: max(top_k * 3, top_k)]
    selected = await _maybe_rerank(query, shortlist, top_k)

    for position, chunk in enumerate(selected, start=1):
        chunk.citation = position

    logger.info(
        "Retrieved %d chunks (pool=%d, rerank=%s)",
        len(selected), len(pool), _rerank_provider(),
    )
    return selected


async def retrieve_context(query: str, mode: str, course_id: str, top_k: int = 5) -> List[str]:
    """Backwards-compatible retrieval returning plain context strings.

    Used by the mode-session flows (practice scenario and review question
    generation), which build their own prompts and do not cite sources.
    """
    chunks = await retrieve(query=query, mode=mode, course_id=course_id, top_k=top_k)
    return [chunk.context_text for chunk in chunks]
