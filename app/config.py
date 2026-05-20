from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_env() -> None:
    """
    Load application configuration in priority order:

    1. Variables already in os.environ (set by the OS / container) — always win.
    2. AWS Secrets Manager (production) — loaded when AWS_SECRET_NAME is set.
    3. .env file (local development fallback).

    The .env file is loaded first so that AWS_SECRET_NAME / AWS_REGION can
    themselves live in .env during local development without being hard-coded.
    """
    # Load .env first (override=False) so existing OS vars are never clobbered.
    # This also makes AWS_SECRET_NAME / AWS_REGION available from .env locally.
    load_dotenv(dotenv_path=_ENV_PATH, override=False)

    # Pull real secrets from AWS Secrets Manager in production.
    # No-op when AWS_SECRET_NAME is not set (local development).
    from app.core.secrets import load_aws_secrets
    load_aws_secrets()
