from app.ai.anthropic_config import client_options


def test_standard_anthropic_key_does_not_add_workspace_header(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)

    assert client_options() == {"api_key": "sk-ant-test"}


def test_identity_linked_key_adds_workspace_header(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")

    assert client_options() == {
        "api_key": "sk-ant-test",
        "default_headers": {"anthropic-workspace-id": "wrkspc_test"},
    }


def test_anthropic_key_is_required(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    try:
        client_options()
    except RuntimeError as exc:
        assert str(exc) == "Missing ANTHROPIC_API_KEY environment variable."
    else:
        raise AssertionError("missing Anthropic key should fail closed")
