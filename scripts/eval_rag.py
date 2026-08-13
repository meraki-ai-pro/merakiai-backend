"""Run the RAG evaluation against a golden set.

    python scripts/eval_rag.py evals/froth-flotation.jsonl
    python scripts/eval_rag.py evals/froth-flotation.jsonl --baseline last.json

The --baseline flag is the point of the script. A single score tells you very
little; a score compared against the last run tells you whether the change you
just made helped, and which specific questions it broke.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_env  # noqa: E402

load_env()

from app.ai.rag.evaluation import load_golden_set, run_evaluation  # noqa: E402

# Below this, a change is noise rather than signal.
MEANINGFUL_DELTA = 0.02


def _compare(current: dict, baseline: dict) -> int:
    """Print the diff against a previous run. Returns a process exit code."""
    cur, base = current["summary"], baseline["summary"]
    print("\n── vs baseline ──")

    regressed = False
    for metric in ("recall_at_k", "mrr", "mean_precision"):
        delta = cur.get(metric, 0) - base.get(metric, 0)
        arrow = "→"
        if delta > MEANINGFUL_DELTA:
            arrow = "▲"
        elif delta < -MEANINGFUL_DELTA:
            arrow = "▼"
            regressed = True
        print(f"  {metric:16} {base.get(metric, 0):.3f} {arrow} {cur.get(metric, 0):.3f}"
              f"  ({delta:+.3f})")

    # The list that actually gets acted on: questions that used to work.
    was_ok = {r["question"] for r in baseline["results"] if r["hit"]}
    now_bad = [r["question"] for r in current["results"] if not r["hit"] and r["question"] in was_ok]
    if now_bad:
        regressed = True
        print(f"\n  {len(now_bad)} question(s) REGRESSED:")
        for q in now_bad:
            print(f"    - {q}")

    newly_ok = [
        r["question"] for r in current["results"]
        if r["hit"] and r["question"] not in was_ok
    ]
    if newly_ok:
        print(f"\n  {len(newly_ok)} question(s) now pass:")
        for q in newly_ok:
            print(f"    + {q}")

    return 1 if regressed else 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate retrieval against a golden set.")
    parser.add_argument("golden_set", help="Path to a .jsonl golden set")
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--out", help="Write the full result JSON here")
    parser.add_argument("--baseline", help="Compare against a previous --out file")
    args = parser.parse_args()

    cases = load_golden_set(args.golden_set)
    if not cases:
        print("Golden set is empty.")
        return 1

    print(f"Running {len(cases)} case(s) at top_k={args.top_k}…\n")
    report = await run_evaluation(cases, top_k=args.top_k)
    summary = report["summary"]

    print(f"  recall@{args.top_k:<8} {summary['recall_at_k']:.3f}")
    print(f"  MRR            {summary['mrr']:.3f}")
    print(f"  precision      {summary['mean_precision']:.3f}")
    print(f"  verdicts       {summary['verdicts']}")

    if summary["failures"]:
        print(f"\n  {len(summary['failures'])} failing case(s):")
        for f in summary["failures"]:
            missed = ", ".join(f["missed"]) or "(nothing usable retrieved)"
            print(f"    - {f['question']}\n        missed: {missed}")

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nWrote {args.out}")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        # Non-zero exit on regression so this can gate a deploy.
        return _compare(report, baseline)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
