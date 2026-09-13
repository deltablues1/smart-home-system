"""
Authentication module for Google Workspace ADK

Provides OAuth 2.0 and Service Account authentication mechanisms
"""

from .oauth_manager import OAuthManager, get_oauth_manager
from .service_account_manager import ServiceAccountManager, get_service_account_manager
from .credential_store import CredentialStore, get_credential_store

__all__ = [
    'OAuthManager',
    'get_oauth_manager',
    'ServiceAccountManager',
    'get_service_account_manager',
    'CredentialStore',
    'get_credential_store',
]
