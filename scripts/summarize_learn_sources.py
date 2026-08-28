"""Print a compact, page-ordered outline of every Learn-mode source file."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.ai.ingestion.math_parser import parse_blocks  # noqa: E402
from scripts.ingest_course_manifest import (  # noqa: E402
    DEFAULT_MANIFEST,
    _source_path,
    load_manifest,
    validate_manifest,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course", required=True)
    parser.add_argument(
        "--file",
        default="",
        help="Optional source filename substring for focused review.",
    )
    parser.add_argument("--max-page-text", type=int, default=220)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    manifest = load_manifest(DEFAULT_MANIFEST)
    root, courses = validate_manifest(
        manifest,
        DEFAULT_MANIFEST,
        [args.course],
        require_source_files=True,
    )
    for course in courses:
        for document in course["documents"]:
            if not document["ingest"] or "learn" not in document["target_modes"]:
                continue
            if args.file and args.file.casefold() not in document["path"].casefold():
                continue
            source = _source_path(root, document["path"])
            blocks = parse_blocks(source.read_bytes(), source.name)
            pages: dict[int, list[dict]] = defaultdict(list)
            for block in blocks:
                pages[int(block.get("page") or 0)].append(block)
            print(
                f"\nDOCUMENT {source.name} | pages={len(pages)} | "
                f"difficulty={document['difficulty']} | topic={document['topic']}"
            )
            for page, page_blocks in sorted(pages.items()):
                fragments: list[str] = []
                equations = 0
                for block in page_blocks:
                    equations += len(block.get("equations") or [])
                    text = " ".join(str(block.get("text") or "").split())
                    if text and text not in fragments:
                        fragments.append(text)
                summary = " | ".join(fragments)
                if len(summary) > args.max_page_text:
                    summary = summary[: args.max_page_text - 1].rstrip() + "…"
                print(f"  PAGE {page:03d} equations={equations:02d}  {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
