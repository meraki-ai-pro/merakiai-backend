import pytest


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


def test_agent_uses_a_current_did_supported_openai_model(monkeypatch):
    from app.media import did_agent_service

    captured = {}

    def fake_post(_url, *, json, **_kwargs):
        captured["payload"] = json
        return FakeResponse(200, {"id": "agent-1"})

    monkeypatch.setattr(did_agent_service.requests, "post", fake_post)
    monkeypatch.setattr("app.core.media_config.get_key", lambda _name: "key")

    assert did_agent_service._create_agent("presenter") == "agent-1"
    assert captured["payload"]["llm"]["model"] == "gpt-4.1-mini"


def test_did_validation_error_cannot_echo_the_openai_key(monkeypatch):
    from app.media import did_agent_service

    secret = "sk-proj-this-must-never-appear-in-a-log-1234567890"
    response = FakeResponse(
        400,
        {
            "kind": "ValidationError",
            "details": {
                "payload.llm": {
                    "value": {"provider": "openai", "api_key": secret},
                    "message": f"invalid nested value {secret}",
                }
            },
        },
    )
    monkeypatch.setattr(
        did_agent_service.requests, "post", lambda *_args, **_kwargs: response
    )
    monkeypatch.setattr("app.core.media_config.get_key", lambda _name: secret)

    with pytest.raises(did_agent_service.DidAgentError) as caught:
        did_agent_service._create_agent("presenter")

    message = str(caught.value)
    assert secret not in message
    assert "[REDACTED]" in message
