"""Concept-video generation across subjects, not just mathematics.

The client asked for video generation to be tested for Biology, Chemistry and
the other courses a Ghanaian university actually runs. Two things were wrong
before this script existed, and it exercises both:

  1. **The lecturer UI sent a hard-coded ``subject="mathematics"`` on every
     render request.** So a Biology or Chemistry concept with no visual style
     chosen was routed to Manim — an animation engine for continuous
     mathematics — and produced a confident, useless equation-shaped video.
     The API now reads the course's own ``subject`` column instead.

  2. **Nothing exercised the Remotion path.** Every render in the pilot so far
     was Manim, so "chemistry routes to remotion" was true in the routing table
     and untested end to end.

Two modes:

    python scripts/check_concept_videos.py
        Offline. Prints the renderer each course name routes to and fails if
        any subject lands somewhere indefensible. No API keys, no network, no
        cost — this is the one to run in CI.

    python scripts/check_concept_videos.py --live --course biology-101
        Queues real renders against a running API and polls until each is ready
        or failed. Needs the API up, a lecturer token, and the render workers
        for BOTH renderers running (see RUNNING_LOCALLY.md §6) — a Remotion
        archetype dispatched with only the Manim worker up sits queued for
        ever, which looks like a hang and is not one.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.media.render.routing import route  # noqa: E402

# Course names as departments actually write them, not tidy subject slugs. The
# router matches these loosely, and that matching is the thing under test —
# "BSc Biochemistry" must not fall through to the default renderer.
COURSE_NAMES = {
    "manim": [
        "Calculus II",
        "Further Mathematics",
        "Statistics and Probability",
        "Quantitative Techniques II",
        "Engineering Mechanics",
        "Physics for Life Sciences",
        "BSc Mathematics",
    ],
    "remotion": [
        "BSc Biology",
        "Organic Chemistry II",
        "Biochemistry I",
        "Human Anatomy and Physiology",
        "Microbiology",
        "Nursing Practice",
        "Pharmacology",
        "General Agriculture",
        "Financial Accounting",
        "Introduction to Economics",
        "Computer Science I",
        "Geology and Mineral Resources",
    ],
}

# One concept per subject family, with the archetype a lecturer would sensibly
# pick. These are the --live payloads.
LIVE_CONCEPTS = [
    {
        "concept_key": "photosynthesis-light-reactions",
        "topic": "Photosynthesis",
        "archetype": "process_flow",
        "source_script": (
            "Walk through the light-dependent reactions of photosynthesis as a "
            "sequence: light strikes photosystem II, water is split releasing "
            "oxygen, electrons pass along the transport chain, a proton gradient "
            "forms across the thylakoid membrane, and ATP synthase produces ATP. "
            "Label each stage and show what enters and leaves it."
        ),
    },
    {
        "concept_key": "titration-curve",
        "topic": "Acid-base chemistry",
        "archetype": "data_story",
        "source_script": (
            "Show the titration curve of a strong acid with a strong base: pH on "
            "the vertical axis against volume of base added. Mark the initial pH, "
            "the buffer region, the steep equivalence point at pH 7, and the "
            "plateau afterwards. Explain why the curve is steep at equivalence."
        ),
    },
    {
        "concept_key": "chain-rule",
        "topic": "Differentiation",
        "archetype": "equation_transform",
        "source_script": (
            "Show how the chain rule differentiates y = (3x^2 + 1)^5: name the "
            "outer and inner function, differentiate each, then multiply and "
            "substitute back."
        ),
    },
    {
        "concept_key": "double-entry-bookkeeping",
        "topic": "Financial accounting",
        "archetype": "ui_or_code_walkthrough",
        "source_script": (
            "Walk through recording one transaction under double entry: a "
            "business buys equipment for cash. Show the debit and the credit "
            "side by side, then show the accounting equation still balancing."
        ),
    },
]

POLL_SECONDS = 15
POLL_TIMEOUT_SECONDS = 20 * 60


def check_routing() -> int:
    """Offline check. Returns a process exit code."""
    from app.media.render.routing import DEFAULT_RENDERER, SUBJECT_DEFAULTS

    failures = []
    print("Renderer routing by course name (no archetype given)\n")

    def matched(name: str) -> bool:
        """Whether the name hit the subject table or fell through.

        Worth distinguishing: an unlisted subject silently gets
        DEFAULT_RENDERER, which is Manim. That is right for a maths course and
        wrong for a Pharmacology one, and it looks identical in the output
        unless you say so.
        """
        key = name.strip().lower()
        return any(subject in key for subject in SUBJECT_DEFAULTS)

    for expected, names in COURSE_NAMES.items():
        for name in names:
            actual = route(None, name)
            ok = actual == expected
            note = "" if matched(name) else f"  (no subject matched; default {DEFAULT_RENDERER})"
            print(f"  {'ok ' if ok else 'BAD'}  {name:<38} -> {actual}{note}")
            if not ok:
                failures.append((name, expected, actual))

    print("\nArchetype overrides subject (the rule that matters most)\n")
    overrides = [
        # A biology course wanting a graph should still get Manim, and a maths
        # course wanting a flowchart should still get Remotion. Subject is only
        # a fallback; locking either one in was the bug this guards against.
        ("plot_animation", "BSc Biology", "manim"),
        ("process_flow", "Calculus II", "remotion"),
        ("data_story", "Physics for Life Sciences", "remotion"),
    ]
    for archetype, subject, expected in overrides:
        actual = route(archetype, subject)
        ok = actual == expected
        print(f"  {'ok ' if ok else 'BAD'}  {archetype:<16} + {subject:<28} -> {actual}")
        if not ok:
            failures.append((f"{archetype}+{subject}", expected, actual))

    if failures:
        print(f"\n{len(failures)} routing decision(s) wrong:")
        for name, expected, actual in failures:
            print(f"  {name}: expected {expected}, got {actual}")
        return 1

    print("\nAll routing decisions correct.")
    return 0


def run_live(course_id: str, base_url: str, token: str) -> int:
    """Queue real renders and wait for them. Returns a process exit code."""
    import requests

    headers = {"Authorization": f"Bearer {token}"}
    queued: list[tuple[str, str]] = []

    for concept in LIVE_CONCEPTS:
        payload = {
            "course_id": course_id,
            # Subject is deliberately NOT sent. The API reads the course's own
            # subject column, and this script exists partly to prove that path
            # works — passing one here would test the thing that was broken.
            **concept,
        }
        response = requests.post(f"{base_url}/render", json=payload, headers=headers, timeout=30)
        if response.status_code != 200:
            print(f"  FAILED to queue {concept['concept_key']}: "
                  f"{response.status_code} {response.text[:200]}")
            continue

        body = response.json()
        asset = body.get("asset", {})
        print(f"  queued {concept['concept_key']:<34} renderer={asset.get('renderer')} "
              f"status={body.get('status')}")
        queued.append((asset["id"], concept["concept_key"]))

    if not queued:
        print("Nothing was queued.")
        return 1

    print(f"\nWaiting for {len(queued)} render(s)…")
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    outstanding = dict(queued)
    results: dict[str, dict] = {}

    while outstanding and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        for asset_id, key in list(outstanding.items()):
            response = requests.get(f"{base_url}/render/{asset_id}", headers=headers, timeout=30)
            if response.status_code != 200:
                continue
            asset = response.json().get("asset", {})
            if asset.get("status") in ("ready", "failed"):
                results[key] = asset
                outstanding.pop(asset_id)
                print(f"  {key}: {asset['status']}"
                      + (f" — {asset.get('error', '')[:200]}" if asset.get("error") else ""))

    print("\nResults")
    failed = 0
    for concept in LIVE_CONCEPTS:
        key = concept["concept_key"]
        asset = results.get(key)
        if not asset:
            # Almost always the render worker for that renderer not running,
            # rather than a slow render — say so rather than "timed out".
            print(f"  {key:<34} still queued after {POLL_TIMEOUT_SECONDS // 60} min "
                  "(is the render worker for this renderer running?)")
            failed += 1
            continue
        if asset["status"] == "failed":
            failed += 1
        # narration_status is reported separately: it is a second job on a
        # different worker, so a 'ready' video with no audio means the media
        # worker is down, not that the render failed.
        print(f"  {key:<34} {asset['status']:<8} renderer={asset.get('renderer'):<9} "
              f"narration={asset.get('narration_status', 'n/a')}")

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="queue real renders")
    parser.add_argument("--course", help="course id to render against (--live)")
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--token", default=os.getenv("LECTURER_TOKEN", ""))
    args = parser.parse_args()

    code = check_routing()
    if not args.live:
        return code

    if not args.course or not args.token:
        print("\n--live needs --course and a lecturer access token "
              "(--token or LECTURER_TOKEN).")
        return 2

    print(f"\nQueueing renders on {args.course} at {args.base_url}\n")
    return run_live(args.course, args.base_url.rstrip("/"), args.token) or code


if __name__ == "__main__":
    raise SystemExit(main())
