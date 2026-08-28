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
