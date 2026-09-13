"""
Credential Store - Abstrakcija za upravljanje vjerodajnicama
Automatski bira između OAuth 2.0 i Service Account ovisno o kontekstu
"""

import os
from typing import Optional, Union
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.oauth2 import service_account
import logging

from .oauth_manager import get_oauth_manager
from .service_account_manager import get_service_account_manager

logger = logging.getLogger(__name__)


class CredentialStore:
    """
    Centralizirana upravljačka klasa za sve vrste Google vjerodajnica

    Automatski bira između:
    - OAuth 2.0 (za korisničke akcije)
    - Service Account (za server-to-server ili Domain-Wide Delegation)
    """

    def __init__(
        self,
        prefer_service_account: bool = False,
        delegated_user: Optional[str] = None
    ):
        """
        Inicijalizacija Credential Store-a

        Args:
            prefer_service_account: Preferiraj Service Account umjesto OAuth-a
            delegated_user: Email za Domain-Wide Delegation (samo za Service Account)
        """
        self.prefer_service_account = prefer_service_account
        self.delegated_user = delegated_user or os.getenv('GOOGLE_WORKSPACE_ADMIN_EMAIL')

        self._oauth_manager = None
        self._service_account_manager = None
        # Which auth path the LAST get_credentials() call actually used —
        # surfaced in health/status so a silent OAuth->SA fallback is visible.
        self.last_auth_mode: Optional[str] = None

    def get_credentials(
        self,
        user_email: Optional[str] = None,
        force_oauth: bool = False,
        force_service_account: bool = False
    ) -> Optional[Union[OAuthCredentials, service_account.Credentials]]:
        """
        Dohvaća najbolje dostupne credentials

        Args:
            user_email: Email korisnika za Domain-Wide Delegation
            force_oauth: Prisili korištenje OAuth-a
            force_service_account: Prisili korištenje Service Account-a

        Returns:
            Credentials objekt ili None ako nisu dostupni

        Prioritet odlučivanja:
        1. Ako je force_oauth=True, pokušaj OAuth
        2. Ako je force_service_account=True, pokušaj Service Account
        3. Ako je prefer_service_account=True i Service Account dostupan, koristi Service Account
        4. Inače pokušaj OAuth
        5. Fallback na Service Account ako OAuth nije dostupan
        """
        # Force OAuth
        if force_oauth:
            creds = self._get_oauth_credentials()
            self.last_auth_mode = "oauth" if creds else None
            return creds

        # Force Service Account
        if force_service_account:
            creds = self._get_service_account_credentials(user_email)
            self.last_auth_mode = "service_account" if creds else None
            return creds

        # Preferiraj Service Account ako je konfiguriran
        if self.prefer_service_account:
            service_creds = self._get_service_account_credentials(user_email)
            if service_creds:
                self.last_auth_mode = "service_account"
                return service_creds

        # Pokušaj OAuth
        oauth_creds = self._get_oauth_credentials()
        if oauth_creds:
            self.last_auth_mode = "oauth"
            return oauth_creds

        # Fallback na Service Account — vidljivo, ne debug: radnje se tada
        # izvršavaju kao service account, a ne kao korisnikov Google račun.
        sa_creds = self._get_service_account_credentials(user_email)
        if sa_creds:
            logger.warning(
                "OAuth credentials unavailable — FALLING BACK to service "
                "account. Actions will run as the service account, not the "
                "user's Google account."
            )
            self.last_auth_mode = "service_account_fallback"
        else:
            self.last_auth_mode = None
        return sa_creds

    def _get_oauth_credentials(self) -> Optional[OAuthCredentials]:
        """Dohvaća OAuth 2.0 credentials"""
        try:
            if not self._oauth_manager:
                self._oauth_manager = get_oauth_manager()

            credentials = self._oauth_manager.get_credentials()
            if credentials:
                logger.debug("Using OAuth 2.0 credentials")
                return credentials
        except Exception as e:
            logger.warning(f"Failed to get OAuth credentials: {e}")

        return None

    def _get_service_account_credentials(
        self,
        user_email: Optional[str] = None
    ) -> Optional[service_account.Credentials]:
        """Dohvaća Service Account credentials"""
        try:
            if not self._service_account_manager:
                self._service_account_manager = get_service_account_manager()

            if not self._service_account_manager:
                return None

            # Koristi proslijeđeni email ili default delegated user
            delegated = user_email or self.delegated_user

            credentials = self._service_account_manager.get_credentials(delegated)
            if credentials:
                logger.debug(
                    f"Using Service Account credentials"
                    + (f" (delegated to {delegated})" if delegated else "")
                )
                return credentials
        except Exception as e:
            logger.warning(f"Failed to get Service Account credentials: {e}")

        return None

    def is_authenticated(self) -> bool:
        """
        Provjerava je li bilo koja vrsta credentials dostupna

        Returns:
            True ako postoje važeći credentials
        """
        return self.get_credentials() is not None

    def get_auth_type(self) -> str:
        """
        Vraća tip trenutne autentifikacije

        Returns:
            'oauth', 'service_account', ili 'none'
        """
        if self._get_oauth_credentials():
            return 'oauth'
        elif self._get_service_account_credentials():
            return 'service_account'
        else:
            return 'none'

    def revoke_oauth(self) -> bool:
        """
        Opoziva OAuth credentials

        Returns:
            True ako je uspješno opozvano
        """
        try:
            if not self._oauth_manager:
                self._oauth_manager = get_oauth_manager()
            return self._oauth_manager.revoke_token()
        except Exception as e:
            logger.error(f"Failed to revoke OAuth token: {e}")
            return False


# Singleton instance
_credential_store_instance: Optional[CredentialStore] = None


def get_credential_store(
    prefer_service_account: bool = False,
    delegated_user: Optional[str] = None
) -> CredentialStore:
    """
    Dohvaća singleton instancu CredentialStore-a

    Args:
        prefer_service_account: Preferiraj Service Account
        delegated_user: Email za Domain-Wide Delegation

    Returns:
        CredentialStore instance
    """
    global _credential_store_instance
    if _credential_store_instance is None:
        _credential_store_instance = CredentialStore(
            prefer_service_account=prefer_service_account,
            delegated_user=delegated_user
        )
    return _credential_store_instance
