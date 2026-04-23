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

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "...":
        print("Missing OPENAI_API_KEY")
        sys.exit(1)

    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - import failure
        print(f"Failed to import openai: {exc}")
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    models = client.models.list()
    count = len(models.data) if hasattr(models, "data") else 0
    print(f"OK: connected, {count} model(s) visible")


if __name__ == "__main__":
    main()
