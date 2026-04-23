from typing import List, Dict

_MODE_MAP = {
    "knowledge": "learn",
    "assessment": "review",
    "practice":   "application",
}

_MIN_CHUNK_WORDS = 40  # discard low-signal fragments


def chunk_text(sections: List[Dict], doc_type: str) -> List[Dict]:
    """
    Paragraph-level chunking for all document types.

    All three modes (learn / review / application) use the same splitting
    strategy: meaningful prose paragraphs of at least _MIN_CHUNK_WORDS words.
    The mode tag is metadata for Pinecone namespace routing — it does NOT
    determine how we split.  The LLM synthesises structured questions or
    scenarios from the retrieved prose at generation time, so the source
    documents can be plain textbooks, notes, or papers in any format.
    """
    if doc_type not in _MODE_MAP:
        raise ValueError(f"Unsupported doc_type: {doc_type!r}. Must be one of {list(_MODE_MAP)}")

    mode = _MODE_MAP[doc_type]
    chunks: List[Dict] = []

    for section in sections:
        topic = section.get("title") or ""
        for para in _split_paragraphs(section["content"]):
            if len(para.split()) < _MIN_CHUNK_WORDS:
                continue
            chunks.append({"text": para, "mode": mode, "topic": topic})

    return chunks


# =====================================================
# HELPERS
# =====================================================

def _split_paragraphs(text: str) -> List[str]:
    """Split on blank lines; strip whitespace; drop empties."""
    return [p.strip() for p in text.split("\n\n") if p.strip()]
