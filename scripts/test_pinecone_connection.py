from __future__ import annotations

import os
import sys
from pathlib import Path


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        print(f"Missing .env at {env_path}")
        sys.exit(1)

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


def main() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_env_file(env_path)

    api_key = os.getenv("PINECONE_API_KEY")
    if not api_key:
        print("Missing PINECONE_API_KEY")
        sys.exit(1)

    try:
        from pinecone import Pinecone
    except Exception as exc:  # pragma: no cover - import failure
        print(f"Failed to import pinecone: {exc}")
        sys.exit(1)

    client = Pinecone(api_key=api_key)
    indexes = client.list_indexes().names()
    print(f"OK: connected, {len(indexes)} index(es) visible")
    if indexes:
        print("Indexes:")
        for name in indexes:
            print(f"- {name}")


if __name__ == "__main__":
    main()
