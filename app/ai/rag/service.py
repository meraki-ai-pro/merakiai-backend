from typing import Any, Dict, List, Union

from .retriever import retrieve_context
from .prompt_builder import build_system_and_user
from .claude import generate_response


async def query_rag(
    user_message: str,
    mode: str,
    course_id: str,
    course_persona: str = None,
    course_domain_topics: List[str] = None,
    memory: List[str] = None,
    top_k: int = 5,
) -> Union[Dict[str, Any], Dict[str, str]]:
    """Full RAG pipeline for a single conversational turn.

    1. Retrieve context from Pinecone (course + mode scoped)
    2. Split prompt into stable system text and dynamic user text
    3. Call Claude with cache_control on the system prefix (prompt caching)
    4. Return structured response dict
    """
    context = await retrieve_context(
        query=user_message,
        mode=mode,
        course_id=course_id,
        top_k=top_k,
    )

    system_text, user_text = build_system_and_user(
        user_message=user_message,
        context=context,
        mode=mode,
        memory=memory,
        course_persona=course_persona,
        course_domain_topics=course_domain_topics,
    )

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

    raw_output = await generate_response(prompt=user_text, mode=mode, system_parts=system_parts)
    return {"mode": mode, "response": raw_output.strip()}
