"""Offline evaluation of retrieval quality.

Retrieval changes are currently judged by asking one question and looking at
the answer. That cannot distinguish "this change helped" from "this question
happened to work", and it silently permits regressions: the hybrid-search fix
earlier in this project improved some queries and could have broken others
without anyone noticing.

This measures instead. A golden set of lecturer-validated questions, each
labelled with the material that *should* come back, scored on:

  recall@k   did the expected source appear at all in the top k?
  MRR        how high up? (1.0 = first result, 0.5 = second, ...)
  precision  what fraction of what came back was relevant?
  answerable did anything usable come back at all?

Ref: AI_Teaching_System_Technical_Specification_v3 §5.3
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)


@dataclass
class GoldenCase:
    """One labelled question.

    ``expect_sources`` are substrings matched against a chunk's filename or
    section title. Substrings rather than ids on purpose: a golden set has to
    survive re-ingestion, and chunk ids change every time the chunker does.
    """

    question: str
    course_id: str
    mode: str = "learn"
    expect_sources: list[str] = field(default_factory=list)
    expect_text: list[str] = field(default_factory=list)
    # A question the material genuinely does not cover. The correct behaviour
    # is to retrieve nothing useful, so scoring it like the others would
    # penalise correct behaviour.
    expect_no_answer: bool = False
    topic: str | None = None
    note: str | None = None


@dataclass
class CaseResult:
    question: str
    hit: bool
    reciprocal_rank: float
    precision: float
    retrieved: int
    best_score: float
    verdict: str
    matched: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)


def _matches(chunk, needle: str) -> bool:
    needle = needle.lower().strip()
    if not needle:
        return False
    haystack = " ".join(
        str(v).lower()
        for v in (
            getattr(chunk, "source_filename", "") or "",
            getattr(chunk, "section_title", "") or "",
            " ".join(getattr(chunk, "heading_path", []) or []),
        )
    )
    return needle in haystack


def _contains_text(chunk, needle: str) -> bool:
    needle = needle.lower().strip()
    body = f"{getattr(chunk, 'text', '')} {getattr(chunk, 'parent_text', '')}".lower()
    return bool(needle) and needle in body


def score_case(case: GoldenCase, chunks: Sequence) -> CaseResult:
    """Score one question against what retrieval returned."""
    from app.ai.rag.crag import assess

    verdict = assess(chunks)

    if case.expect_no_answer:
        # Success here is the ABSENCE of a confident match. Scoring it with the
        # same rule as the others would mark correct behaviour as a failure.
        clean = verdict.verdict != "correct"
        return CaseResult(
            question=case.question,
            hit=clean,
            reciprocal_rank=1.0 if clean else 0.0,
            precision=1.0 if clean else 0.0,
            retrieved=len(chunks),
            best_score=verdict.best_score,
            verdict=verdict.verdict,
        )

    expectations = list(case.expect_sources) + list(case.expect_text)
    if not expectations:
        # Unlabelled: only "did anything usable come back" can be judged.
        answered = verdict.verdict != "incorrect"
        return CaseResult(
            question=case.question,
            hit=answered,
            reciprocal_rank=1.0 if answered else 0.0,
            precision=1.0 if answered else 0.0,
            retrieved=len(chunks),
            best_score=verdict.best_score,
            verdict=verdict.verdict,
        )

    matched: list[str] = []
    first_rank: int | None = None
    relevant_positions: set[int] = set()

    for needle in case.expect_sources:
        for i, chunk in enumerate(chunks):
            if _matches(chunk, needle):
                matched.append(needle)
                relevant_positions.add(i)
                first_rank = i + 1 if first_rank is None else min(first_rank, i + 1)
                break

    for needle in case.expect_text:
        for i, chunk in enumerate(chunks):
            if _contains_text(chunk, needle):
                matched.append(needle)
                relevant_positions.add(i)
                first_rank = i + 1 if first_rank is None else min(first_rank, i + 1)
                break

    missed = [e for e in expectations if e not in matched]

    return CaseResult(
        question=case.question,
        hit=bool(matched),
        reciprocal_rank=round(1.0 / first_rank, 4) if first_rank else 0.0,
        precision=round(len(relevant_positions) / len(chunks), 4) if chunks else 0.0,
        retrieved=len(chunks),
        best_score=verdict.best_score,
        verdict=verdict.verdict,
        matched=matched,
        missed=missed,
    )


def aggregate(results: Sequence[CaseResult]) -> dict[str, Any]:
    if not results:
        return {"cases": 0}
    n = len(results)
    return {
        "cases": n,
        "recall_at_k": round(sum(1 for r in results if r.hit) / n, 4),
        "mrr": round(sum(r.reciprocal_rank for r in results) / n, 4),
        "mean_precision": round(sum(r.precision for r in results) / n, 4),
        "mean_best_score": round(sum(r.best_score for r in results) / n, 4),
        "verdicts": {
            v: sum(1 for r in results if r.verdict == v)
            for v in ("correct", "ambiguous", "incorrect")
        },
        "failures": [
            {"question": r.question, "missed": r.missed, "verdict": r.verdict}
            for r in results
            if not r.hit
        ],
    }


def load_golden_set(path: str | Path) -> list[GoldenCase]:
    """Read a JSONL golden set. One JSON object per line.

    JSONL rather than one big JSON array so a lecturer can append a case with
    a text editor and a malformed line fails loudly on its own line number
    instead of invalidating the whole file.
    """
    cases: list[GoldenCase] = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        try:
            cases.append(GoldenCase(**json.loads(line)))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
    return cases


async def run_evaluation(
    cases: Sequence[GoldenCase], *, top_k: int = 6
) -> dict[str, Any]:
    """Run every case against live retrieval and score it."""
    from app.ai.rag.retriever import retrieve

    results: list[CaseResult] = []
    for case in cases:
        try:
            chunks = await retrieve(
                query=case.question, mode=case.mode, course_id=case.course_id, top_k=top_k
            )
        except Exception as exc:  # noqa: BLE001 — one bad case must not stop the run
            logger.warning("Case failed: %s — %s", case.question, exc)
            results.append(
                CaseResult(case.question, False, 0.0, 0.0, 0, 0.0, "error")
            )
            continue
        results.append(score_case(case, chunks))

    return {
        "summary": aggregate(results),
        "results": [asdict(r) for r in results],
    }
