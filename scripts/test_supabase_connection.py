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

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        sys.exit(1)

    try:
        from supabase import create_client
    except Exception as exc:  # pragma: no cover - import failure
        print(f"Failed to import supabase: {exc}")
        sys.exit(1)

    client = create_client(url, key)
    response = client.table("users").select("*").limit(1).execute()
    rows = response.data or []
    print(f"OK: connected, fetched {len(rows)} row(s)")


if __name__ == "__main__":
    main()
