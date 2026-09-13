"""
OAuth 2.0 Manager for Google Workspace APIs.
"""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

logger = logging.getLogger(__name__)


class OAuthManager:
    """Manage OAuth 2.0 authentication and token refresh."""

    SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/contacts",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/tasks",
        "https://www.googleapis.com/auth/adwords",
        "https://www.googleapis.com/auth/youtube.upload",
        # NOTE: cloud-platform and datastore are intentionally NOT requested on
        # the user OAuth token. They trigger Google's reauth (RAPT) policy, which
        # forces interactive re-login roughly daily and breaks headless token
        # refresh ("Reauthentication is needed ... gcloud auth ..."). Vertex AI
        # (Gemini/grounding) and Firestore use the Service Account, not this
        # user token, so dropping these scopes is safe.
    ]

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        token_storage_path: Optional[str] = None,
    ):
        self.client_id = client_id or os.getenv("GOOGLE_OAUTH_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
        self.redirect_uri = redirect_uri or os.getenv(
            "GOOGLE_OAUTH_REDIRECT_URI",
            "http://localhost:8080/oauth2callback",
        )

        resolved_token_path = (
            token_storage_path
            or os.getenv("OAUTH_TOKEN_STORAGE_PATH")
            or os.path.join(Path.home(), ".google_workspace_adk", "tokens.json")
        )
        self.token_storage_path = str(Path(resolved_token_path).expanduser())
        Path(self.token_storage_path).parent.mkdir(parents=True, exist_ok=True)

        self._credentials: Optional[Credentials] = None
        # Serializes token load/refresh so concurrent callers don't trigger
        # duplicate refresh requests. Reentrant: _refresh_credentials is called
        # from within get_credentials while the lock is held.
        self._lock = threading.RLock()
        self._last_auth_health: Dict[str, Any] = {
            "status": "relogin_required",
            "token_path": self.token_storage_path,
            "reason": "not_checked",
        }
        self._pending_flow: Optional[Flow] = None

    def _client_config(self) -> Dict[str, Dict[str, Any]]:
        return {
            "web": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uris": [self.redirect_uri],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }

    def _build_flow(self) -> Flow:
        """Create a configured OAuth flow instance."""
        return Flow.from_client_config(
            self._client_config(),
            scopes=self.SCOPES,
            redirect_uri=self.redirect_uri,
        )

    def get_authorization_url(self) -> str:
        flow = self._build_flow()
        # Reuse the same flow during token exchange so the PKCE code_verifier survives.
        self._pending_flow = flow
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            # Keep the issued token to exactly SCOPES. With "true", a previously
            # granted cloud-platform scope would be folded back in via incremental
            # auth and re-trigger the reauth policy we are removing.
            include_granted_scopes="false",
            prompt="consent",
        )
        return auth_url

    def exchange_code_for_token(self, authorization_code: str) -> Credentials:
        # Reuse the flow created in get_authorization_url so the PKCE
        # code_verifier matches; a fresh Flow here would fail the exchange.
        flow = self._pending_flow
        if flow is None:
            raise RuntimeError(
                "OAuth authorization flow state is missing. Start authorization and "
                "exchange the callback code in the same process."
            )

        try:
            flow.fetch_token(code=authorization_code)
            self._credentials = flow.credentials
        finally:
            self._pending_flow = None

        self._save_token()
        self._last_auth_health = {
            "status": "valid",
            "token_path": self.token_storage_path,
        }
        logger.info("Successfully exchanged authorization code for token")
        return self._credentials

    def get_credentials(self) -> Optional[Credentials]:
        with self._lock:
            if self._credentials and self._credentials.valid:
                self._last_auth_health = {
                    "status": "valid",
                    "token_path": self.token_storage_path,
                }
                return self._credentials

            self._load_token()

            if not self._credentials:
                self._last_auth_health = {
                    "status": "relogin_required",
                    "token_path": self.token_storage_path,
                    "reason": "token_missing",
                }
                return None

            if not self._credentials.expiry and self._credentials.refresh_token:
                logger.info("Token loaded without expiry, forcing refresh")
                return self._refresh_credentials(reason="missing_expiry")

            if self._credentials.expired and self._credentials.refresh_token:
                return self._refresh_credentials(reason="expired")

            if self._credentials.valid:
                self._last_auth_health = {
                    "status": "valid",
                    "token_path": self.token_storage_path,
                }
                return self._credentials

            self._last_auth_health = {
                "status": "relogin_required",
                "token_path": self.token_storage_path,
                "reason": "invalid_credentials",
            }
            return None

    def _refresh_credentials(self, reason: str) -> Optional[Credentials]:
        try:
            self._credentials.refresh(Request())
            self._save_token()
            self._last_auth_health = {
                "status": "refreshed",
                "token_path": self.token_storage_path,
                "reason": reason,
            }
            logger.info("Successfully refreshed OAuth token")
            return self._credentials
        except Exception as e:
            logger.error(f"Failed to refresh token: {e}")
            self._last_auth_health = {
                "status": "relogin_required",
                "token_path": self.token_storage_path,
                "reason": f"refresh_failed: {e}",
            }
            self._credentials = None
            return None

    def get_auth_health_status(self) -> Dict[str, Any]:
        self.get_credentials()
        return dict(self._last_auth_health)

    def _save_token(self) -> None:
        if not self._credentials:
            return

        token_data = {
            "token": self._credentials.token,
            "refresh_token": self._credentials.refresh_token,
            "token_uri": self._credentials.token_uri,
            "client_id": self._credentials.client_id,
            "client_secret": self._credentials.client_secret,
            "scopes": self._credentials.scopes,
            "expiry": self._credentials.expiry.isoformat() if self._credentials.expiry else None,
        }

        try:
            with open(self.token_storage_path, "w", encoding="utf-8") as f:
                json.dump(token_data, f)
            logger.debug(f"Token saved to {self.token_storage_path}")
        except Exception as e:
            logger.error(f"Failed to save token: {e}")

    def _load_token(self) -> None:
        if not os.path.exists(self.token_storage_path):
            return

        try:
            with open(self.token_storage_path, "r", encoding="utf-8") as f:
                token_data = json.load(f)

            expiry = None
            expiry_str = token_data.get("expiry")
            if expiry_str:
                expiry = datetime.fromisoformat(expiry_str)
                if expiry.tzinfo is not None:
                    expiry = expiry.replace(tzinfo=None)

            self._credentials = Credentials(
                token=token_data.get("token"),
                refresh_token=token_data.get("refresh_token"),
                token_uri=token_data.get("token_uri"),
                client_id=token_data.get("client_id"),
                client_secret=token_data.get("client_secret"),
                scopes=token_data.get("scopes"),
                expiry=expiry,
            )
            logger.debug(f"Token loaded from {self.token_storage_path}")
        except Exception as e:
            logger.error(f"Failed to load token: {e}")

    def revoke_token(self) -> bool:
        credentials = self.get_credentials()
        if not credentials:
            return False

        try:
            import requests

            revoke = requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": credentials.token},
                headers={"content-type": "application/x-www-form-urlencoded"},
            )

            if revoke.status_code == 200:
                if os.path.exists(self.token_storage_path):
                    os.remove(self.token_storage_path)
                self._credentials = None
                self._last_auth_health = {
                    "status": "relogin_required",
                    "token_path": self.token_storage_path,
                    "reason": "revoked",
                }
                logger.info("Token successfully revoked")
                return True

            logger.error(f"Failed to revoke token: {revoke.status_code}")
            return False
        except Exception as e:
            logger.error(f"Error revoking token: {e}")
            return False

    def is_authenticated(self) -> bool:
        return self.get_credentials() is not None


_oauth_manager_instance: Optional[OAuthManager] = None


def get_oauth_manager() -> OAuthManager:
    global _oauth_manager_instance
    if _oauth_manager_instance is None:
        try:
            from config.auth_config import get_auth_config

            oauth_cfg = get_auth_config().oauth
            _oauth_manager_instance = OAuthManager(
                client_id=oauth_cfg.client_id,
                client_secret=oauth_cfg.client_secret,
                redirect_uri=oauth_cfg.redirect_uri,
                token_storage_path=oauth_cfg.token_storage_path,
            )
        except Exception:
            _oauth_manager_instance = OAuthManager()
    return _oauth_manager_instance
