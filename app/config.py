import os

def load_env():
    env = os.getenv("APP_ENV", "development")
    if env == "production":
        _load_from_secrets_manager()
    else:
        from dotenv import load_dotenv
        load_dotenv()

def _load_from_secrets_manager():
    import json
    import boto3
    from botocore.exceptions import ClientError

    secret_arn = os.environ["AWS_SECRET_ARN"]  # Set via systemd EnvironmentFile
    client = boto3.client("secretsmanager", region_name="us-east-1")
    try:
        response = client.get_secret_value(SecretId=secret_arn)
    except ClientError as e:
        raise RuntimeError(f"Failed to load secrets: {e}") from e

    secrets = json.loads(response["SecretString"])
    for key, value in secrets.items():
        os.environ.setdefault(key, str(value))