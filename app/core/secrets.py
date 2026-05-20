"""
AWS Secrets Manager loader.

─── Production setup ────────────────────────────────────────────────────────
1. Store ALL application secrets as a single JSON object in AWS Secrets Manager:
       aws secretsmanager create-secret \
           --name meraki-backend/prod \
           --secret-string file://secrets.json

2. Set TWO environment variables on the production server/container (these are
   the only values that should ever live outside AWS):
       AWS_SECRET_NAME=meraki-backend/prod
       AWS_REGION=us-east-1

3. Grant the EC2 / ECS task IAM role the secretsmanager:GetSecretValue
   permission for that secret ARN. No AWS_ACCESS_KEY_ID needed on the host.

─── Local development ────────────────────────────────────────────────────────
Leave AWS_SECRET_NAME unset. The loader becomes a no-op and the .env file is
used as normal.

─── How it works ─────────────────────────────────────────────────────────────
All key-value pairs in the secret JSON are injected into os.environ so the
rest of the application reads them transparently via os.getenv(), exactly as
it would from a .env file. No other code needs to change.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)


def load_aws_secrets() -> None:
    """Fetch secrets from AWS Secrets Manager and inject into os.environ.

    Safe to call multiple times — values already present in os.environ are
    never overridden, so explicit container-level env vars always win.
    """
    secret_name = os.getenv("AWS_SECRET_NAME")
    if not secret_name:
        # Local development — .env file is used instead.
        return

    region = os.getenv("AWS_REGION", "us-east-1")

    try:
        import boto3
        from botocore.exceptions import ClientError  # noqa: F401

        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_name)
        secret_str = response.get("SecretString") or ""

        if not secret_str:
            raise ValueError("SecretString is empty — check the secret value in AWS")

        secrets: dict = json.loads(secret_str)

        injected = 0
        for key, value in secrets.items():
            if key not in os.environ:
                os.environ[key] = str(value)
                injected += 1

        logger.info(
            "AWS Secrets Manager: loaded %d secret(s) from '%s' (region=%s)",
            injected,
            secret_name,
            region,
        )

    except Exception as exc:
        # Fail loudly at startup — a misconfigured secret is never safe to ignore.
        raise RuntimeError(
            f"Failed to load secrets from AWS Secrets Manager "
            f"(secret='{secret_name}', region='{region}'): {exc}"
        ) from exc
