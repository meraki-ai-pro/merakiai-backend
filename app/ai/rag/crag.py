"""Corrective RAG: notice when retrieval failed, and do something about it.

Today a weak retrieval is invisible. The model receives five barely-relevant
passages, is told to ground its answer in them, and produces something fluent
and wrong — which is the worst failure mode a teaching system has, because the
student cannot tell.

Two corrections, in order of cost:

1. **Query rewriting.** Student questions are conversational ("wait so what
   happens if the bottom goes to zero?") while course material is formal
   ("the limit does not exist where the denominator vanishes"). A rewrite into
   the register of the source material fixes a large share of misses for one
   cheap model call.

2. **Honest failure.** When retrieval is still weak after a rewrite, say so.
   A grounded "the notes do not cover this" is more useful to a student — and
   far more useful to the lecturer reading retrieval.empty events — than a
   confident answer assembled from unrelated passages.

Ref: Next_Gen_RAG_Features_and_UI_UX (CRAG), Integration Roadmap §A.2
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)

# Confidence thresholds over the best chunk's score. Deliberately coarse: the
# underlying score is a cosine similarity or a re-ranker output, neither of
# which supports finer distinctions than "clearly relevant / maybe / no".
STRONG = float(os.getenv("CRAG_STRONG", "0.62"))
WEAK = float(os.getenv("CRAG_WEAK", "0.38"))

# Wall-clock ceiling on the whole rewrite-and-retry. Measured as the largest
# single contributor to slow turns: a rewrite is one model call plus a second
# full retrieval, and a marginally better ordering is not worth doubling the
# student's wait. Past this, the first retrieval stands.
RETRY_BUDGET = float(os.getenv("CRAG_RETRY_BUDGET_MS", "2000")) / 1000

# Do not start a retry with less than this left on the turn's deadline. A
# rewrite is a model call before the second retrieval even begins, so starting
# with a sliver of budget guarantees a timeout the student pays for and gains
# nothing.
MIN_RETRY_WINDOW = float(os.getenv("CRAG_MIN_RETRY_WINDOW_MS", "1200")) / 1000

CORRECT = "correct"      # good enough — answer normally
AMBIGUOUS = "ambiguous"  # worth a rewrite and a second attempt
INCORRECT = "incorrect"  # nothing usable — say so rather than improvise


def _enabled() -> bool:
    return os.getenv("RAG_CRAG", "1").strip().lower() not in ("0", "false", "off", "no")


@dataclass(frozen=True)
class Assessment:
    verdict: str
    best_score: float
    mean_score: float
    usable: int

    @property
    def should_retry(self) -> bool:
        return self.verdict == AMBIGUOUS

    @property
    def should_admit_failure(self) -> bool:
        return self.verdict == INCORRECT


def assess(chunks: Sequence) -> Assessment:
    """Grade a retrieval before it is used.

    Judged on the BEST chunk, not the mean. One strongly relevant passage is
    enough to answer from; a mean is dragged down by the long tail of the
    candidate pool and would condemn perfectly good retrievals.
    """
    if not chunks:
        return Assessment(INCORRECT, 0.0, 0.0, 0)

    scores = [
        c.rerank_score if getattr(c, "rerank_score", None) is not None else c.dense_score
        for c in chunks
    ]
    best = max(scores)
    mean = sum(scores) / len(scores)
    usable = sum(1 for s in scores if s >= WEAK)

    if best >= STRONG:
        verdict = CORRECT
    elif best >= WEAK:
        verdict = AMBIGUOUS
    else:
        verdict = INCORRECT

    return Assessment(verdict, round(best, 4), round(mean, 4), usable)


_REWRITE_SYSTEM = """You rewrite a student's question so it matches the wording \
of formal course notes.

Rules:
- Output ONLY the rewritten query. No preamble, no quotes, no explanation.
- Use the technical vocabulary a textbook would use for this topic.
- Keep it to one line, under 30 words.
- Preserve the actual question. Do not answer it, generalise it, or narrow it \
to something easier.
- If the question is already in formal terms, return it essentially unchanged."""


async def rewrite_query(question: str, course_topics: list[str] | None = None) -> str:
    """Restate a conversational question in the register of the source material.

    Returns the original on any failure. A rewrite is an optimisation; losing
    it must never lose the turn.
    """
    from app.ai.rag.claude import generate_response

    context = ""
    if course_topics:
        context = f"\nCourse covers: {', '.join(course_topics[:8])}"

    try:
        out = await generate_response(
            prompt=f"Student question: {question}{context}",
            mode="review_generation",  # small, fast, cheap — this is not teaching
            system_parts=[{"type": "text", "text": _REWRITE_SYSTEM}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Query rewrite failed, using the original: %s", exc)
        return question

    # Split lines BEFORE stripping quotes. The other order leaves the closing
    # quote attached when the model adds a trailing sentence, because that
    # quote is no longer at the end of the string.
    first_line = (out or "").strip().split("\n")[0]
    rewritten = first_line.strip().strip('"').strip("'").strip()

    # A rewrite that collapsed to nothing, or ballooned, is a bad rewrite.
    if not rewritten or len(rewritten) > 300:
        return question
    return rewritten


# Appended to the reference block when retrieval could not support an answer.
# Phrased as an instruction about honesty rather than a refusal, so the model
# still helps where it legitimately can.
INSUFFICIENT_CONTEXT_DIRECTIVE = """
RETRIEVAL WAS WEAK FOR THIS QUESTION.

The reference material above may not actually cover what was asked. Before
answering:

- If the material does not address the question, say so plainly in one
  sentence, then offer what the course DOES cover nearby so the student has
  somewhere to go.
- Do not assemble an answer out of passages that are only loosely related, and
  do not fill the gap from general knowledge while implying it came from the
  course notes.
- Suggest they ask their lecturer if this is examinable material.
"""
