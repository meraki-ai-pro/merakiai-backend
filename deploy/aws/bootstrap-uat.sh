#!/usr/bin/env bash
set -Eeuo pipefail

# One-time bootstrap for a dedicated MerakiAI UAT EC2 instance. Invoked by the
# UAT GitHub Actions deployment through SSM; no SSH key is required.

umask 027

commit_sha="${1:-}"
secret_arn="${2:-}"
aws_region="${AWS_REGION:-us-east-1}"
app_user="meraki"
app_root="/srv/meraki-uat"
app_dir="${app_root}/merakiai-backend"
venv_dir="${app_root}/.venv"

if [[ ! "$commit_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "A full 40-character lowercase Git commit SHA is required" >&2
  exit 64
fi
if [[ ! "$secret_arn" =~ ^arn:aws:secretsmanager: ]]; then
  echo "A UAT Secrets Manager ARN is required" >&2
  exit 64
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git jq nginx python3 python3-venv rabbitmq-server redis-server

if ! id "$app_user" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$app_root" \
    --shell /usr/sbin/nologin "$app_user"
fi
install -d -o "$app_user" -g "$app_user" -m 0750 "$app_root"

if [[ ! -d "$app_dir/.git" ]]; then
  sudo -u "$app_user" git clone --filter=blob:none \
    https://github.com/meraki-ai-pro/merakiai-backend.git "$app_dir"
fi
sudo -u "$app_user" git -C "$app_dir" fetch --prune origin dev
sudo -u "$app_user" git -C "$app_dir" cat-file -e "${commit_sha}^{commit}"
if ! sudo -u "$app_user" git -C "$app_dir" merge-base --is-ancestor \
  "$commit_sha" FETCH_HEAD; then
  echo "Commit is not reachable from origin/dev" >&2
  exit 65
fi
sudo -u "$app_user" git -C "$app_dir" checkout --detach "$commit_sha"

if [[ ! -x "$venv_dir/bin/python" ]]; then
  sudo -u "$app_user" python3 -m venv "$venv_dir"
fi
sudo -u "$app_user" "$venv_dir/bin/python" -m pip install \
  --require-hashes --requirement "$app_dir/requirements.lock.txt"

install -d -o root -g root -m 0750 /etc/meraki
cat >/etc/meraki/backend-uat.env <<EOF
APP_ENV=production
AWS_REGION=${aws_region}
AWS_SECRET_ARN=${secret_arn}
OPENAI_EMBEDDING_MODEL=text-embedding-3-large
OPENAI_EMBEDDING_DIMENSIONS=3072
EOF
chown root:root /etc/meraki/backend-uat.env
chmod 0640 /etc/meraki/backend-uat.env

install -d -o root -g root -m 0750 /etc/ssl/meraki-uat
AWS_REGION="$aws_region" AWS_SECRET_ARN="$secret_arn" \
  "$venv_dir/bin/python" - <<'PY'
import json
import os
from pathlib import Path

import boto3

client = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])
payload = client.get_secret_value(SecretId=os.environ["AWS_SECRET_ARN"])
secret = json.loads(payload["SecretString"])
for key, destination in (
    ("CLOUDFLARE_ORIGIN_CERT_PEM", "/etc/ssl/meraki-uat/origin.crt"),
    ("CLOUDFLARE_ORIGIN_KEY_PEM", "/etc/ssl/meraki-uat/origin.key"),
):
    value = str(secret.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"{key} is missing from the UAT secret")
    path = Path(destination)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
PY

install -o root -g root -m 0755 \
  "$app_dir/deploy/aws/meraki-deploy" /usr/local/sbin/meraki-deploy
install -o root -g root -m 0640 \
  "$app_dir/deploy/aws/deploy-uat.conf.example" /etc/meraki/deploy-uat.conf
install -o root -g root -m 0644 \
  "$app_dir/deploy/aws/systemd/"*"-uat.service" /etc/systemd/system/
install -o root -g root -m 0644 \
  "$app_dir/deploy/aws/nginx/merakiai-api-uat.conf" \
  /etc/nginx/sites-available/merakiai-api-uat
ln -sfn /etc/nginx/sites-available/merakiai-api-uat \
  /etc/nginx/sites-enabled/merakiai-api-uat
rm -f /etc/nginx/sites-enabled/default

systemctl enable --now redis-server rabbitmq-server
systemctl daemon-reload
systemctl enable meraki-api-uat meraki-celery-text-uat \
  meraki-celery-video-uat meraki-celery-ingestion-uat
systemctl restart meraki-api-uat meraki-celery-text-uat \
  meraki-celery-video-uat meraki-celery-ingestion-uat
nginx -t
systemctl enable --now nginx
systemctl restart nginx

curl --fail --silent --show-error --retry 12 --retry-delay 5 \
  --retry-all-errors http://127.0.0.1:8001/health >/dev/null
touch /etc/meraki/uat-bootstrapped
echo "UAT bootstrap complete for ${commit_sha}"
