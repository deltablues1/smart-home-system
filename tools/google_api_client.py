"""
Google API Client Wrapper

Base wrapper za Google API pozive s credential management
Omogućava jednostavno pozivanje Google APIs s automatskom autentifikacijom
"""

import os
from typing import Optional, Any, Dict
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import logging
from dotenv import load_dotenv
from config.google_runtime import get_gemini_location

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)


async def aexecute(request: Any) -> Any:
    """Execute a googleapiclient HttpRequest without blocking the event loop.

    googleapiclient is synchronous — calling .execute() directly inside an
    async function freezes the whole asyncio loop (web server, SSE, voice
    websocket) for the duration of the HTTP call. Always await this instead:

        result = await aexecute(service.files().list(...))
    """
    import asyncio
    return await asyncio.to_thread(request.execute)


def get_vertex_ai_config() -> Dict[str, str]:
    """
    Get Vertex AI configuration from environment variables

    Returns:
        Dictionary with Vertex AI configuration:
        - project_id: Google Cloud Project ID
        - location: Gemini runtime location (default: global)
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise ValueError("GOOGLE_CLOUD_PROJECT environment variable not set")

    location = get_gemini_location(default="global")

    return {
        "project_id": project_id,
        "location": location
    }


class GoogleAPIClient:
    """
    Base wrapper za Google API pozive

    Omogućava:
    - Automatsku credential management
    - Resource building za različite Google APIs
    - Error handling
    - Logging
    """

    # API verzije za različite servise
    API_VERSIONS = {
        'gmail': 'v1',
        'drive': 'v3',
        'docs': 'v1',
        'sheets': 'v4',
        'calendar': 'v3',
        'people': 'v1',  # Contacts API
        'tasks': 'v1',
    }

    def __init__(self, credentials: Optional[Credentials] = None):
        """
        Inicijalizacija Google API Client-a

        Args:
            credentials: Google OAuth2/Service Account credentials
        """
        self.credentials = credentials
        self._resources: Dict[str, Any] = {}  # Cache za API resources

    def get_service(self, service_name: str, version: Optional[str] = None) -> Any:
        """
        Dohvaća Google API service resource

        Args:
            service_name: Naziv servisa (gmail, drive, docs, sheets, calendar, people, tasks)
            version: API verzija (default: koristi API_VERSIONS)

        Returns:
            Google API Resource objekt

        Raises:
            ValueError: Ako credentials nisu postavljeni
            HttpError: Ako dohvaćanje servisa faila
        """
        if not self.credentials:
            raise ValueError("Credentials not set. Call set_credentials() first.")

        # Koristi default verziju ako nije specificirana
        if not version:
            version = self.API_VERSIONS.get(service_name, 'v1')

        # Cache key
        cache_key = f"{service_name}_{version}"

        # Provjeri cache
        if cache_key in self._resources:
            logger.debug(f"Using cached resource: {cache_key}")
            return self._resources[cache_key]

        # Kreiraj novi resource
        try:
            logger.info(f"Building Google API resource: {service_name} {version}")
            resource = build(service_name, version, credentials=self.credentials)
            self._resources[cache_key] = resource
            return resource

        except HttpError as e:
            logger.error(f"Failed to build {service_name} API resource: {e}")
            raise

    def set_credentials(self, credentials: Credentials) -> None:
        """
        Postavlja credentials

        Args:
            credentials: Google OAuth2/Service Account credentials
        """
        self.credentials = credentials
        # Očisti cache jer credentials su promijenjeni
        self._resources.clear()
        logger.debug("Credentials updated, resource cache cleared")

    def clear_cache(self) -> None:
        """Briše cache API resources"""
        self._resources.clear()
        logger.debug("API resource cache cleared")

    # ========================================================================
    # CONVENIENCE METHODS - Brži pristup za česte API operacije
    # ========================================================================

    def gmail_service(self) -> Any:
        """Dohvaća Gmail API service"""
        return self.get_service('gmail')

    def drive_service(self) -> Any:
        """Dohvaća Drive API service"""
        return self.get_service('drive')

    def docs_service(self) -> Any:
        """Dohvaća Docs API service"""
        return self.get_service('docs')

    def sheets_service(self) -> Any:
        """Dohvaća Sheets API service"""
        return self.get_service('sheets')

    def calendar_service(self) -> Any:
        """Dohvaća Calendar API service"""
        return self.get_service('calendar')

    def people_service(self) -> Any:
        """Dohvaća People API service (Contacts)"""
        return self.get_service('people')

    def tasks_service(self) -> Any:
        """Dohvaća Tasks API service"""
        return self.get_service('tasks')

    def __repr__(self) -> str:
        creds_type = "None"
        if self.credentials:
            if isinstance(self.credentials, service_account.Credentials):
                creds_type = "ServiceAccount"
            elif isinstance(self.credentials, Credentials):
                creds_type = "OAuth2"
        return f"<GoogleAPIClient(credentials={creds_type}, cached_services={len(self._resources)})>"


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def create_api_client_from_oauth_manager(oauth_manager) -> GoogleAPIClient:
    """
    Kreira GoogleAPIClient iz OAuthManager instance

    Args:
        oauth_manager: OAuthManager instance

    Returns:
        GoogleAPIClient instance

    Raises:
        ValueError: Ako credentials nisu dostupni
    """
    credentials = oauth_manager.get_credentials()
    if not credentials:
        raise ValueError("No valid OAuth credentials available. Please authenticate first.")

    client = GoogleAPIClient(credentials=credentials)
    logger.info("Created GoogleAPIClient from OAuthManager")
    return client


def create_api_client_from_service_account_manager(sa_manager, delegated_user: Optional[str] = None) -> GoogleAPIClient:
    """
    Kreira GoogleAPIClient iz ServiceAccountManager instance

    Args:
        sa_manager: ServiceAccountManager instance
        delegated_user: Email korisnika za Domain-Wide Delegation

    Returns:
        GoogleAPIClient instance
    """
    credentials = sa_manager.get_credentials(delegated_user=delegated_user)
    client = GoogleAPIClient(credentials=credentials)
    logger.info("Created GoogleAPIClient from ServiceAccountManager")
    return client


def create_api_client_auto() -> GoogleAPIClient:
    """
    Automatski kreira GoogleAPIClient na osnovu dostupnih credentials

    Pokušava po ovom redoslijedu:
    1. OAuth credentials
    2. Service Account credentials

    Returns:
        GoogleAPIClient instance

    Raises:
        ValueError: Ako nijedna autentifikacija nije dostupna
    """
    # Pokušaj OAuth
    try:
        from auth.oauth_manager import get_oauth_manager

        oauth_manager = get_oauth_manager()
        credentials = oauth_manager.get_credentials()

        if credentials:
            logger.info("Using OAuth credentials for API client")
            return GoogleAPIClient(credentials=credentials)
    except Exception as e:
        logger.debug(f"OAuth credentials not available: {e}")

    # Pokušaj Service Account
    try:
        from auth.service_account_manager import get_service_account_manager

        sa_manager = get_service_account_manager()
        if sa_manager:
            credentials = sa_manager.get_credentials()
            logger.info("Using Service Account credentials for API client")
            return GoogleAPIClient(credentials=credentials)
    except Exception as e:
        logger.debug(f"Service Account credentials not available: {e}")

    raise ValueError(
        "No authentication available. Please configure OAuth or Service Account credentials."
    )


# Global singleton instance (optional - može se koristiti ili ne)
_global_api_client: Optional[GoogleAPIClient] = None


def get_global_api_client() -> GoogleAPIClient:
    """
    Dohvaća global singleton GoogleAPIClient

    Returns:
        GoogleAPIClient instance

    Raises:
        ValueError: Ako credentials nisu dostupni
    """
    global _global_api_client
    if _global_api_client is None:
        _global_api_client = create_api_client_auto()
    return _global_api_client


def set_global_api_client(client: GoogleAPIClient) -> None:
    """
    Postavlja global GoogleAPIClient

    Args:
        client: GoogleAPIClient instance
    """
    global _global_api_client
    _global_api_client = client
    logger.debug("Global API client set")
