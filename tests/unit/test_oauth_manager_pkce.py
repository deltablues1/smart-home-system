from unittest.mock import Mock, patch

from auth.oauth_manager import OAuthManager


def test_exchange_code_reuses_pending_flow_for_pkce(tmp_path):
    token_path = tmp_path / "tokens.json"
    manager = OAuthManager(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8080/oauth2callback",
        token_storage_path=str(token_path),
    )

    flow = Mock()
    flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth?...", "state-123")
    flow.credentials = Mock()

    with patch.object(manager, "_build_flow", return_value=flow):
        auth_url = manager.get_authorization_url()
        creds = manager.exchange_code_for_token("auth-code-123")

    assert auth_url.startswith("https://accounts.google.com/o/oauth2/auth")
    flow.fetch_token.assert_called_once_with(code="auth-code-123")
    assert creds is flow.credentials
    assert manager._pending_flow is None


def test_exchange_code_requires_existing_flow_state(tmp_path):
    manager = OAuthManager(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8080/oauth2callback",
        token_storage_path=str(tmp_path / "tokens.json"),
    )

    try:
        manager.exchange_code_for_token("auth-code-123")
    except RuntimeError as exc:
        assert "flow state is missing" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError when PKCE flow state is missing")
