import tempfile
from pathlib import Path

from auth.oauth_manager import OAuthManager


def test_oauth_manager_uses_env_token_storage_path(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        token_path = Path(tmp_dir) / "oauth" / "tokens.json"
        monkeypatch.setenv("OAUTH_TOKEN_STORAGE_PATH", str(token_path))

        manager = OAuthManager()

        assert Path(manager.token_storage_path) == token_path
        assert token_path.parent.exists()


def test_oauth_health_reports_relogin_when_token_missing(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        token_path = Path(tmp_dir) / "missing.json"
        monkeypatch.setenv("OAUTH_TOKEN_STORAGE_PATH", str(token_path))

        manager = OAuthManager()
        health = manager.get_auth_health_status()

        assert health["status"] == "relogin_required"
        assert health["reason"] == "token_missing"
