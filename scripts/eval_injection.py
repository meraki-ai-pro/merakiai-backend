"""Prompt-injection regression set: real model calls, no database.

    python scripts/eval_injection.py evals/prompt-injection.jsonl

Exits non-zero if any case fails, so it can gate a prompt change the way
eval_rag.py gates a retrieval change. Costs a handful of short API calls.
A pass is evidence, not proof: models are not deterministic.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_env  # noqa: E402

load_env()

from app.ai.rag.claude import generate_response  # noqa: E402
from app.ai.rag.prompt_builder import build_system_and_user  # noqa: E402
from app.api.v1.assessments import suggest_mark  # noqa: E402

CONTEXT = [
    "Froth flotation separates hydrophobic particles from hydrophilic ones. Air bubbles "
    "attach to hydrophobic particles and carry them to the froth layer. Collectors make "
    "mineral surfaces hydrophobic; frothers stabilise the bubbles."
]
QUESTION = {
    "id": "eval", "points": 4,
    "prompt": "What does a collector do in froth flotation?",
    "correct_answer": "It adsorbs on mineral surfaces and makes them hydrophobic so bubbles can carry them.",
}


async def run_case(case: dict) -> list[str]:
    problems: list[str] = []
    if case["kind"] == "tutor":
        system, user = build_system_and_user(case["question"], CONTEXT, "learn")
        out = await generate_response(user, "learn", system_parts=[{"type": "text", "text": system}])
        low = out.lower()
        for bad in case.get("must_not_contain", []):
            if bad.lower() in low:
                problems.append(f"answer contains {bad!r}")
        wanted = case.get("must_contain_any")
        if wanted and not any(w.lower() in low for w in wanted):
            problems.append(f"answer lacks any of {wanted}")
    else:
        score, _feedback, ok = await suggest_mark(QUESTION, case["answer"])
        if "max_score" in case and score > case["max_score"]:
            problems.append(f"score {score} > {case['max_score']}")
        if "min_score" in case and score < case["min_score"]:
            problems.append(f"score {score} < {case['min_score']}")
        if "expect_ok" in case and ok != case["expect_ok"]:
            problems.append(f"ok={ok}, expected {case['expect_ok']}")
    return problems


async def main(path: str) -> int:
    cases = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    failed = 0
    for case in cases:
        problems = await run_case(case)
        status = "FAIL" if problems else "ok  "
        print(f"{status} [{case['kind']}] {case.get('note', '')}")
        for p in problems:
            print(f"       {p}")
        failed += bool(problems)
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(sys.argv[1])))
