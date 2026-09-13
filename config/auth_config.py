"""
Authentication Configuration

Centralna konfiguracija za OAuth 2.0 i Service Account
"""

import os
from typing import Optional
from dataclasses import dataclass


@dataclass
class OAuthConfig:
    """OAuth 2.0 konfiguracija"""

    client_id: str
    client_secret: str
    redirect_uri: str = "http://localhost:8080/oauth2callback"
    token_storage_path: Optional[str] = None

    @classmethod
    def from_env(cls) -> 'OAuthConfig':
        """Učitaj OAuth config iz environment varijabli"""
        return cls(
            client_id=os.getenv('GOOGLE_OAUTH_CLIENT_ID', ''),
            client_secret=os.getenv('GOOGLE_OAUTH_CLIENT_SECRET', ''),
            redirect_uri=os.getenv('GOOGLE_OAUTH_REDIRECT_URI', 'http://localhost:8080/oauth2callback'),
            token_storage_path=os.getenv('OAUTH_TOKEN_STORAGE_PATH'),
        )


@dataclass
class ServiceAccountConfig:
    """Service Account konfiguracija"""

    credentials_file: str
    delegated_user: Optional[str] = None
    project_id: Optional[str] = None

    @classmethod
    def from_env(cls) -> 'ServiceAccountConfig':
        """Učitaj Service Account config iz environment varijabli"""
        return cls(
            credentials_file=os.getenv('GOOGLE_APPLICATION_CREDENTIALS', ''),
            delegated_user=os.getenv('GOOGLE_WORKSPACE_ADMIN_EMAIL'),
            project_id=os.getenv('GOOGLE_CLOUD_PROJECT'),
        )


@dataclass
class AuthConfig:
    """Glavna auth konfiguracija"""

    prefer_service_account: bool = False
    use_secret_manager: bool = False
    secret_manager_project: Optional[str] = None
    encryption_key: Optional[str] = None

    oauth: Optional[OAuthConfig] = None
    service_account: Optional[ServiceAccountConfig] = None

    @classmethod
    def from_env(cls) -> 'AuthConfig':
        """Učitaj auth config iz environment varijabli"""
        prefer_sa = os.getenv('PREFER_SERVICE_ACCOUNT', 'false').lower() == 'true'
        use_sm = os.getenv('USE_SECRET_MANAGER', 'false').lower() == 'true'

        return cls(
            prefer_service_account=prefer_sa,
            use_secret_manager=use_sm,
            secret_manager_project=os.getenv('SECRET_MANAGER_PROJECT'),
            encryption_key=os.getenv('ENCRYPTION_KEY'),
            oauth=OAuthConfig.from_env(),
            service_account=ServiceAccountConfig.from_env(),
        )


# Singleton instance
_auth_config: Optional[AuthConfig] = None


def get_auth_config() -> AuthConfig:
    """
    Dohvaća singleton instancu AuthConfig-a

    Returns:
        AuthConfig instance
    """
    global _auth_config
    if _auth_config is None:
        _auth_config = AuthConfig.from_env()
    return _auth_config
