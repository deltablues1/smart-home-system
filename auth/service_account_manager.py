"""
Service Account Manager za Google Workspace API
Implementira Domain-Wide Delegation za server-to-server autentifikaciju
"""

import os
import json
from typing import Optional, List
from google.oauth2 import service_account
from google.auth.transport.requests import Request
import logging

logger = logging.getLogger(__name__)


class ServiceAccountManager:
    """Upravlja Service Account autentifikacijom za Google Workspace API"""

    # OAuth 2.0 scopes za Google Workspace
    SCOPES = [
        # Gmail
        'https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/gmail.send',
        'https://www.googleapis.com/auth/gmail.modify',
        'https://www.googleapis.com/auth/gmail.compose',
        # Drive
        'https://www.googleapis.com/auth/drive',
        'https://www.googleapis.com/auth/drive.file',
        # Docs
        'https://www.googleapis.com/auth/documents',
        # Sheets
        'https://www.googleapis.com/auth/spreadsheets',
        # Calendar
        'https://www.googleapis.com/auth/calendar',
        'https://www.googleapis.com/auth/calendar.events',
        # Contacts
        'https://www.googleapis.com/auth/contacts',
        'https://www.googleapis.com/auth/contacts.readonly',
        # Tasks
        'https://www.googleapis.com/auth/tasks',
    ]

    def __init__(
        self,
        service_account_file: Optional[str] = None,
        scopes: Optional[List[str]] = None
    ):
        """
        Inicijalizacija Service Account Manager-a

        Args:
            service_account_file: Putanja do service account JSON datoteke
            scopes: Lista OAuth 2.0 scopes (default: sve workspace scopes)
        """
        self.service_account_file = service_account_file or os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
        self.scopes = scopes or self.SCOPES

        if not self.service_account_file:
            raise ValueError(
                "Service account file not provided. Set GOOGLE_APPLICATION_CREDENTIALS environment variable."
            )

        if not os.path.exists(self.service_account_file):
            raise FileNotFoundError(f"Service account file not found: {self.service_account_file}")

        self._credentials = None
        self._load_credentials()

    def _load_credentials(self) -> None:
        """Učitava service account credentials iz datoteke"""
        try:
            self._credentials = service_account.Credentials.from_service_account_file(
                self.service_account_file,
                scopes=self.scopes
            )
            logger.info("Service account credentials loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load service account credentials: {e}")
            raise

    def get_credentials(self, delegated_user: Optional[str] = None) -> service_account.Credentials:
        """
        Dohvaća service account credentials

        Args:
            delegated_user: Email adresa korisnika za Domain-Wide Delegation
                          (potrebno za pristup korisničkim resursima)

        Returns:
            Service Account Credentials objekt
        """
        if not self._credentials:
            self._load_credentials()

        credentials = self._credentials

        # Domain-Wide Delegation - delegira pristup određenom korisniku
        if delegated_user:
            credentials = credentials.with_subject(delegated_user)
            logger.debug(f"Credentials delegated to user: {delegated_user}")

        # Osvježi credentials ako je potrebno
        if not credentials.valid:
            try:
                credentials.refresh(Request())
                logger.debug("Service account credentials refreshed")
            except Exception as e:
                logger.error(f"Failed to refresh service account credentials: {e}")
                raise

        return credentials

    def get_project_id(self) -> str:
        """
        Dohvaća Google Cloud Project ID iz service account datoteke

        Returns:
            Project ID
        """
        try:
            with open(self.service_account_file, 'r') as f:
                service_account_info = json.load(f)
            return service_account_info.get('project_id', '')
        except Exception as e:
            logger.error(f"Failed to read project ID from service account file: {e}")
            return ''

    def get_service_account_email(self) -> str:
        """
        Dohvaća service account email adresu

        Returns:
            Service account email
        """
        try:
            with open(self.service_account_file, 'r') as f:
                service_account_info = json.load(f)
            return service_account_info.get('client_email', '')
        except Exception as e:
            logger.error(f"Failed to read service account email: {e}")
            return ''

    def is_valid(self) -> bool:
        """
        Provjerava jesu li credentials važeći

        Returns:
            True ako su credentials važeći
        """
        if not self._credentials:
            return False

        if not self._credentials.valid:
            try:
                self._credentials.refresh(Request())
            except Exception:
                return False

        return self._credentials.valid


# Singleton instance
_service_account_manager_instance: Optional[ServiceAccountManager] = None


def get_service_account_manager() -> ServiceAccountManager:
    """
    Dohvaća singleton instancu ServiceAccountManager-a

    Returns:
        ServiceAccountManager instance
    """
    global _service_account_manager_instance
    if _service_account_manager_instance is None:
        try:
            _service_account_manager_instance = ServiceAccountManager()
        except (ValueError, FileNotFoundError) as e:
            logger.warning(f"Service Account Manager not available: {e}")
            return None
    return _service_account_manager_instance
