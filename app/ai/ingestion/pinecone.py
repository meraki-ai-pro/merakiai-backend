import os
import uuid
import json
from pinecone import Pinecone

from app.config import load_env

load_env()

api_key = os.getenv("PINECONE_API_KEY")
index_name = os.getenv("PINECONE_INDEX") or os.getenv("PINECONE_INDEX_NAME")

if not api_key:
    raise RuntimeError("PINECONE_API_KEY is not set")

if not index_name:
    raise RuntimeError("PINECONE_INDEX is not set")

pc = Pinecone(api_key=api_key)
available_indexes = pc.list_indexes().names()

if index_name not in available_indexes:
    raise RuntimeError(
        f"Pinecone index '{index_name}' not found. "
        "Create it in Pinecone or update PINECONE_INDEX/PINECONE_INDEX_NAME."
    )

index = pc.Index(index_name)


def _normalize_embedding(embedding):
    if isinstance(embedding, str):
        embedding = json.loads(embedding)
    return [float(value) for value in embedding]


def _chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def upsert_vectors(embeddings, chunks, namespace):
    ids = []
    vectors = []

    for emb, chunk in zip(embeddings, chunks):
        pid = str(uuid.uuid4())
        ids.append(pid)
        emb = _normalize_embedding(emb)
        topic = chunk.get("topic")
        if topic is None:
            topic = ""
        vectors.append(
            (
                pid,
                emb,
                {"mode": chunk["mode"], "topic": topic, "text": chunk.get("text", "")},
            )
        )

    # Pinecone recommended batch size is ~100 vectors for metadata-heavy upserts
    batch_size = 100
    for batch in _chunk_list(vectors, batch_size):
        index.upsert(vectors=batch, namespace=namespace)

    return ids


def delete_vectors(ids: list[str], namespace: str) -> None:
    """Delete vectors from Pinecone by ID within a namespace."""
    if not ids:
        return
    for batch in _chunk_list(ids, 1000):
        index.delete(ids=batch, namespace=namespace)
