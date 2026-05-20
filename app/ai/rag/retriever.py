import asyncio
import hashlib
import json
import os
import logging
from typing import List

from openai import AsyncOpenAI
from pinecone import Pinecone

from app.db.supabase import get_async_supabase

logger = logging.getLogger(__name__)

# Lazy singletons
_openai_client: AsyncOpenAI | None = None
_pinecone_index = None


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


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_embedding(embedding):
    """Ensure embedding is a list[float]."""
    if isinstance(embedding, str):
        embedding = json.loads(embedding)
    return [float(v) for v in embedding]


async def _get_cached_embedding(text: str):
    """Async check for cache."""
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
    """Async store for cache."""
    supabase = await get_async_supabase()
    text_hash = _hash_text(text)
    try:
        await supabase.table("embedding_cache").insert(
            {"text_hash": text_hash, "embedding": embedding}
        ).execute()
    except Exception as e:
        # Ignore duplicate key errors or other DB hiccups
        logger.warning(f"Failed to store in embedding cache: {e}")


async def retrieve_context(query: str, mode: str, course_id: str, top_k: int = 5) -> List[str]:
    """
    Retrieves top_k chunks from Pinecone for the given course and mode.

    Namespace pattern: "{course_id}-{mode}"  e.g. "froth-flotation-learn"
    This isolates each course's content so retrieval never crosses course
    boundaries regardless of what mode the student is in.
    """
    mode = (mode or "learn").lower().strip()
    course_id = (course_id or "").strip()
    if not course_id:
        raise ValueError("course_id is required for retrieve_context")
    logger.info(f"Retrieving context  course={course_id}  mode={mode}")

    # Cache check
    embedding = await _get_cached_embedding(query)

    if embedding is None:
        logger.debug("Cache miss for embedding. Calling OpenAI.")
        client = _get_openai()
        try:
            response = await client.embeddings.create(
                model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large"),
                input=query,
            )
            embedding = response.data[0].embedding
            await _store_embedding(query, embedding)
        except Exception as e:
            logger.error(f"OpenAI embedding creation failed: {e}")
            raise

    embedding = _normalize_embedding(embedding)

    index = _get_pinecone_index()

    # Pinecone query is blocking, wrap in to_thread
    try:
        results = await asyncio.to_thread(
            index.query,
            vector=embedding,
            top_k=top_k,
            include_metadata=True,
            namespace=f"{course_id}-{mode}",
            filter={"mode": {"$eq": mode}},
        )
    except Exception as e:
        logger.error(f"Pinecone query failed: {e}")
        return []

    chunks: List[str] = []
    for match in results.get("matches", []):
        metadata = match.get("metadata") or {}
        text = metadata.get("text") or metadata.get("content")
        if text:
            chunks.append(text)

    logger.info(f"Retrieved {len(chunks)} chunks from Pinecone.")
    return chunks
