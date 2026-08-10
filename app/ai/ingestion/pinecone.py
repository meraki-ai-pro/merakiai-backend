import json
import logging
import os
import uuid
from typing import Any, Dict, List, Sequence

from pinecone import Pinecone

from app.ai.ingestion.structured_chunker import to_pinecone_metadata

logger = logging.getLogger(__name__)

# Lazy singleton. This module used to build the client and call list_indexes()
# at *import* time — a network round-trip on the import path of every process
# that touched ingestion, and an unrecoverable crash if Pinecone happened to be
# unreachable while the app was starting.
_index = None


def _get_index():
    global _index
    if _index is not None:
        return _index

    api_key = os.getenv("PINECONE_API_KEY")
    index_name = os.getenv("PINECONE_INDEX") or os.getenv("PINECONE_INDEX_NAME")
    if not api_key:
        raise RuntimeError("PINECONE_API_KEY is not set")
    if not index_name:
        raise RuntimeError("PINECONE_INDEX is not set")

    pc = Pinecone(api_key=api_key)
    if index_name not in pc.list_indexes().names():
        raise RuntimeError(
            f"Pinecone index '{index_name}' not found. "
            "Create it in Pinecone or update PINECONE_INDEX/PINECONE_INDEX_NAME."
        )
    _index = pc.Index(index_name)
    return _index


def _normalize_embedding(embedding):
    if isinstance(embedding, str):
        embedding = json.loads(embedding)
    return [float(value) for value in embedding]


def _chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# Pinecone caps metadata at 40KB per vector. parent_text is the only field that
# can approach it — the chunker bounds parents already, but a pathological
# document should degrade rather than fail the whole upsert.
_MAX_METADATA_BYTES = 38_000


def _fit_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    encoded = len(json.dumps(metadata).encode("utf-8"))
    if encoded <= _MAX_METADATA_BYTES:
        return metadata

    trimmed = dict(metadata)
    parent = trimmed.get("parent_text", "")
    if parent:
        overflow = encoded - _MAX_METADATA_BYTES
        trimmed["parent_text"] = parent[: max(0, len(parent) - overflow - 200)]
        trimmed["parent_truncated"] = True
        logger.warning(
            "Trimmed oversized parent_text on chunk %s", trimmed.get("chunk_index")
        )
    return trimmed


def upsert_chunks(
    embeddings: Sequence[Sequence[float]],
    chunks: List[Dict[str, Any]],
    namespace: str,
    batch_size: int = 100,
    mode: str | None = None,
) -> List[str]:
    """Upsert parent-child chunks together with their full citation metadata.

    Uses each chunk's own deterministic id rather than a fresh uuid4, so
    re-ingesting a document overwrites its vectors instead of duplicating them.

    ``mode`` overrides the mode stamped into each vector's metadata. A document
    tagged for several modes is embedded once and upserted into one namespace
    per mode; the retriever filters on ``metadata.mode`` inside the namespace,
    so vectors landing in the review namespace must say "review" even though
    the chunk was built with the document's default mode. Namespaces are
    separate keyspaces, so the shared deterministic ids do not collide.
    """
    if len(embeddings) != len(chunks):
        raise ValueError(
            f"embedding/chunk count mismatch: {len(embeddings)} vs {len(chunks)}"
        )

    index = _get_index()
    ids: List[str] = []
    vectors = []

    for embedding, chunk in zip(embeddings, chunks):
        ids.append(chunk["id"])
        metadata = to_pinecone_metadata(chunk)
        if mode:
            metadata["mode"] = mode
        vectors.append((
            chunk["id"],
            _normalize_embedding(embedding),
            _fit_metadata(metadata),
        ))

    for batch in _chunk_list(vectors, batch_size):
        index.upsert(vectors=batch, namespace=namespace)

    logger.info("Upserted %d vectors into namespace %s", len(ids), namespace)
    return ids


def upsert_vectors(embeddings, chunks, namespace):
    """Legacy upsert for the old ``{text, mode, topic}`` chunk shape."""
    index = _get_index()
    ids = []
    vectors = []

    for emb, chunk in zip(embeddings, chunks):
        pid = str(uuid.uuid4())
        ids.append(pid)
        vectors.append((
            pid,
            _normalize_embedding(emb),
            {
                "mode": chunk["mode"],
                "topic": chunk.get("topic") or "",
                "text": chunk.get("text", ""),
            },
        ))

    for batch in _chunk_list(vectors, 100):
        index.upsert(vectors=batch, namespace=namespace)

    return ids


def delete_vectors(ids: list[str], namespace: str) -> None:
    """Delete vectors from Pinecone by ID within a namespace."""
    if not ids:
        return
    index = _get_index()
    for batch in _chunk_list(ids, 1000):
        index.delete(ids=batch, namespace=namespace)
