import os

from openai import AsyncOpenAI

# Lazy singleton. This module used to read the key and construct the client at
# *import* time, raising when it was absent — which made every module that
# transitively imports ingestion unimportable without a full environment, and
# turned a missing key into an import-time crash rather than a request-time
# error. Matches the pattern in retriever.py and pinecone.py.
_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing OPENAI_API_KEY environment variable.")
        _client = AsyncOpenAI(api_key=api_key)
    return _client


async def embed_chunks(texts):
    if not texts:
        return []
    response = await _get_client().embeddings.create(
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large"), input=texts
    )
    return [d.embedding for d in response.data]
