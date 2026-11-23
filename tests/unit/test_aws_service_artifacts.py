from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_DIR = ROOT / "deploy" / "aws" / "systemd"


def test_production_service_units_match_deploy_contract():
    deploy_config = (ROOT / "deploy" / "aws" / "deploy-production.conf.example").read_text()
    expected = {
        "meraki-api",
        "meraki-celery-text",
        "meraki-celery-video",
        "meraki-celery-ingestion",
    }

    for service in expected:
        assert service in deploy_config
        unit = (SYSTEMD_DIR / f"{service}.service").read_text()
        assert "User=meraki" in unit
        assert "EnvironmentFile=/etc/meraki/backend.env" in unit
        assert "NoNewPrivileges=true" in unit


def test_api_and_nginx_keep_uvicorn_private():
    api_unit = (SYSTEMD_DIR / "meraki-api.service").read_text()
    nginx = (ROOT / "deploy" / "aws" / "nginx" / "merakiai-api.conf").read_text()

    assert "--host 127.0.0.1" in api_unit
    assert "proxy_pass http://127.0.0.1:8000" in nginx
    assert "listen 443 ssl" in nginx
    assert "ssl_protocols TLSv1.2 TLSv1.3" in nginx


def test_text_workers_have_capacity_for_the_200_student_ai_burst():
    production = (SYSTEMD_DIR / "meraki-celery-text.service").read_text()
    uat = (SYSTEMD_DIR / "meraki-celery-text-uat.service").read_text()
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "--concurrency=8" in production
    assert "--concurrency=8" in uat
    assert "UAT_AI_WORKER_CONCURRENCY || '8'" in workflow
    assert "--worker-concurrency $AI_WORKER_CONCURRENCY" in workflow


def test_deploy_validates_against_the_branch_it_just_fetched():
    deploy = (ROOT / "deploy" / "aws" / "meraki-deploy").read_text()

    assert 'fetch --prune "$GIT_REMOTE" "$GIT_BRANCH"' in deploy
    assert '"$commit_sha" FETCH_HEAD' in deploy
    assert '"$GIT_REMOTE/$GIT_BRANCH"' not in deploy


def test_deploy_refreshes_and_rolls_back_service_units():
    deploy = (ROOT / "deploy" / "aws" / "meraki-deploy").read_text()

    assert "refresh_service_units()" in deploy
    assert 'unit="$source_dir/deploy/aws/systemd/${service}.service"' in deploy
    assert "systemctl daemon-reload" in deploy
    assert deploy.count('refresh_service_units "$APP_DIR"') == 2


def test_production_workflow_bootstraps_the_versioned_deploy_helper():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "raw.githubusercontent.com/meraki-ai-pro/merakiai-backend/${{ github.sha }}" in workflow
    assert "sudo install --owner root --group root --mode 0755" in workflow


def test_uat_deploy_refreshes_versioned_edge_configuration():
    deploy = (ROOT / "deploy" / "aws" / "meraki-deploy").read_text()
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert 'refresh_uat_edge "$APP_DIR"' in deploy
    assert "CLOUDFLARE_ORIGIN_CERT_PEM" in deploy
    assert "merakiai-api-uat.conf" in deploy
    assert workflow.count("/tmp/meraki-deploy") == 4


def test_uat_hostname_is_covered_by_the_origin_certificate():
    nginx = (ROOT / "deploy" / "aws" / "nginx" / "merakiai-api-uat.conf").read_text()

    assert "server_name uat-api.merakiai.online;" in nginx
    assert "server_name uat.api.merakiai.online;" not in nginx


def test_deployment_waits_long_enough_for_a_cold_renderer_build():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "aws ssm wait command-executed" not in workflow
    assert workflow.count("for attempt in $(seq 1 60)") == 2
    assert workflow.count("for attempt in $(seq 1 150)") == 1
    assert workflow.count("for attempt in $(seq 1 330)") == 1
    assert workflow.count("Success|Failed|Cancelled|TimedOut|Cancelling") == 4
