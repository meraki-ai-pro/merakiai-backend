from __future__ import annotations

import os


_REQUIRED_PRODUCTION_SETTINGS = (
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_JWT_SECRET",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "PINECONE_API_KEY",
    "PINECONE_INDEX",
    "REDIS_URL",
    "RABBITMQ_URL",
    "ALLOWED_ORIGINS",
    "ALLOWED_HOSTS",
    "PUBLIC_SITE_URL",
)


def _validate_production_settings() -> None:
    missing = [name for name in _REQUIRED_PRODUCTION_SETTINGS if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError(
            "Missing required production configuration: " + ", ".join(missing)
        )


def load_env() -> None:
    environment = os.getenv("APP_ENV", "development").strip().lower()
    if environment == "production":
        if not (os.getenv("AWS_SECRET_ARN") or os.getenv("AWS_SECRET_NAME")):
            raise RuntimeError(
                "APP_ENV=production requires AWS_SECRET_ARN or AWS_SECRET_NAME"
            )
        from app.core.secrets import load_aws_secrets

        load_aws_secrets()
        _validate_production_settings()
        return

    from dotenv import load_dotenv

    load_dotenv()
