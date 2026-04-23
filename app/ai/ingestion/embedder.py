import os
from openai import AsyncOpenAI

from app.config import load_env

load_env()
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("Missing OPENAI_API_KEY environment variable.")

# Use AsyncOpenAI
client = AsyncOpenAI(api_key=api_key)


async def embed_chunks(texts):
    if not texts:
        return []
    response = await client.embeddings.create(
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large"), input=texts
    )
    return [d.embedding for d in response.data]
