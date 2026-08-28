# AWS deployment contract

Production and UAT deploy through GitHub Actions → AWS OIDC → SSM Run Command.
No SSH key or long-lived AWS access key belongs in GitHub.

## One-time EC2 bootstrap

After the instance inventory and paths have been verified, install the versioned
deploy command and one root-owned configuration per environment:

```bash
sudo install -o root -g root -m 0755 \
  /srv/meraki/merakiai-backend/deploy/aws/meraki-deploy \
  /usr/local/sbin/meraki-deploy
sudo install -o root -g root -m 0640 \
  /srv/meraki/merakiai-backend/deploy/aws/deploy-production.conf.example \
  /etc/meraki/deploy-production.conf

sudo install -o root -g root -m 0644 \
  /srv/meraki/merakiai-backend/deploy/aws/systemd/*.service \
  /etc/systemd/system/
sudo install -o root -g root -m 0644 \
  /srv/meraki/merakiai-backend/deploy/aws/nginx/merakiai-api.conf \
  /etc/nginx/sites-available/merakiai-api
sudo systemctl daemon-reload
```

Edit the installed configuration to match the real checkout, virtualenv,
branch, health URL, user, and systemd units. Repeat with the UAT example only
if UAT actually exists. The script refuses dirty worktrees, checks that the
requested 40-character SHA belongs to the configured remote branch, deploys
that exact tested commit, and rolls back code/dependencies/services when the
local health check fails.

The instance profile needs `AmazonSSMManagedInstanceCore` and read-only access
to its Secrets Manager secret. Set only these bootstrap values in the systemd
environment file:

```dotenv
APP_ENV=production
AWS_REGION=us-east-1
AWS_SECRET_ARN=arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:prod/meraki/config-SUFFIX
OPENAI_EMBEDDING_MODEL=text-embedding-3-large
OPENAI_EMBEDDING_DIMENSIONS=3072
```

Keep the embedding model and dimensions explicit in `/etc/meraki/backend.env`.
The production startup guard intentionally refuses to boot when either value is
missing, preventing queries from reaching a Pinecone index with a mismatched
vector shape.

The JSON secret must follow `.env.example`. In particular, use
`SUPABASE_SERVICE_ROLE_KEY`; `SUPABASE_KEY` is not read by this application.

The four application services bind only to loopback. Nginx is the sole public
origin listener and terminates TLS with the certificate paths declared in
`deploy/aws/nginx/merakiai-api.conf`. Keep port 8000 closed in the EC2 security
group. When the DNS record is proxied, restrict port 443 to Cloudflare's
published IPv4 and IPv6 ranges and retain Systems Manager as the administrative
path instead of opening SSH.

Manim and Remotion run as two hardened Docker Compose services declared in
`deploy/aws/docker-compose.render.production.yml`. The containers use host
networking only so they can reach the loopback-only Redis and RabbitMQ services;
neither exposes a listening port. `/etc/meraki/render.env` must be root-owned,
mode `0600`, and contain only `REDIS_URL`, `RABBITMQ_URL`, `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, and `ANTHROPIC_API_KEY`. Do not copy the complete
application secret into this file.

The deploy command tags both images with the tested commit SHA, rebuilds them
after checkout, and keeps the previous tag available for rollback. Verify both
queue consumers after deployment:

```bash
sudo rabbitmqctl list_consumers -p meraki_vhost queue_name consumer_tag -q \
  | grep -E 'render_(manim|remotion)'
```

`/etc/meraki/backend.env` is intentionally small and root-owned:

```dotenv
APP_ENV=production
AWS_REGION=us-east-1
AWS_SECRET_ARN=arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:prod/meraki/config-SUFFIX
```

The API service starts two Uvicorn workers. Text, video, and ingestion queues
have dedicated Celery workers so long-running media or document work cannot
starve student answers. Concept-video render workers remain containerized using
the dedicated render Dockerfiles; do not run generated Manim code in these host
services.

## GitHub OIDC role

Create an AWS IAM role whose trust policy is limited to the two GitHub
environments used by this repository:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "ForAnyValue:StringEquals": {
        "token.actions.githubusercontent.com:sub": [
          "repo:meraki-ai-pro/merakiai-backend:environment:uat",
          "repo:meraki-ai-pro/merakiai-backend:environment:production"
        ]
      }
    }
  }]
}
```

Its permissions should allow `ssm:SendCommand` only against the verified EC2
instance and AWS-managed `AWS-RunShellScript` document, plus
`ssm:GetCommandInvocation`/`ssm:ListCommandInvocations` for result polling.

## GitHub environments and variables

Create `uat` and `production` environments. Require a reviewer on production.
Define these environment or repository variables (they are identifiers, not
secrets):

- `AWS_ROLE_ARN`
- `AWS_REGION`
- `EC2_INSTANCE_ID`
- `UAT_API_URL`
- `PROD_API_URL`

The `dev` branch deploys to UAT only after tests, dependency audit, and Docker
build pass. The `main` branch follows the same checks and then waits at the
production environment approval gate.
