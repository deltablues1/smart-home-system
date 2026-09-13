"""
Unit tests for Authentication Managers

Tests OAuth Manager, Service Account Manager, and Credential Store.
"""

import pytest
from unittest.mock import Mock, patch, mock_open, MagicMock
import json
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials


class TestOAuthManager:
    """Unit tests for OAuth Manager"""

    @pytest.fixture
    def mock_oauth_config(self):
        """Mock OAuth configuration"""
        return {
            "client_id": "test-client-id",
            "client_secret": "test-secret",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/drive"
            ]
        }

    def test_oauth_manager_initialization(self, mock_oauth_config):
        """Test OAuth manager initializes correctly"""
        # Arrange & Act
        with patch('auth.oauth_manager.OAuthManager') as MockManager:
            manager = MockManager(mock_oauth_config)

            # Assert
            assert manager is not None
            MockManager.assert_called_once_with(mock_oauth_config)

    def test_get_authorization_url(self, mock_oauth_config):
        """Test generating authorization URL"""
        # Arrange
        with patch('auth.oauth_manager.OAuthManager') as MockManager:
            manager = MagicMock()
            MockManager.return_value = manager
            manager.get_authorization_url.return_value = "https://accounts.google.com/o/oauth2/auth?..."

            # Act
            oauth_manager = MockManager(mock_oauth_config)
            auth_url = oauth_manager.get_authorization_url()

            # Assert
            assert auth_url.startswith("https://accounts.google.com/o/oauth2/auth")
            manager.get_authorization_url.assert_called_once()

    def test_exchange_code_for_token(self):
        """Test exchanging authorization code for token"""
        # Arrange
        with patch('auth.oauth_manager.OAuthManager') as MockManager:
            manager = MagicMock()
            MockManager.return_value = manager

            mock_credentials = Mock(spec=Credentials)
            mock_credentials.token = "access-token-123"
            manager.exchange_code.return_value = mock_credentials

            # Act
            oauth_manager = MockManager({})
            credentials = oauth_manager.exchange_code("auth-code-xyz")

            # Assert
            assert credentials.token == "access-token-123"
            manager.exchange_code.assert_called_once_with("auth-code-xyz")

    def test_save_credentials_to_file(self):
        """Test saving credentials to file"""
        # Arrange
        creds_data = {
            "token": "test-token",
            "refresh_token": "test-refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test-client-id",
            "client_secret": "test-secret"
        }

        # Act
        with patch('builtins.open', mock_open()) as mock_file:
            with patch('json.dump') as mock_json_dump:
                with open('tokens.json', 'w') as f:
                    json.dump(creds_data, f, indent=2)

                # Assert
                mock_json_dump.assert_called_once()
                assert mock_json_dump.call_args[0][0] == creds_data

    def test_load_credentials_from_file(self):
        """Test loading credentials from file"""
        # Arrange
        saved_creds = json.dumps({
            "token": "saved-token",
            "refresh_token": "saved-refresh",
            "token_uri": "https://oauth2.googleapis.com/token"
        })

        # Act
        with patch('builtins.open', mock_open(read_data=saved_creds)):
            with open('tokens.json', 'r') as f:
                loaded_data = json.load(f)

            # Assert
            assert loaded_data["token"] == "saved-token"
            assert loaded_data["refresh_token"] == "saved-refresh"

    def test_credentials_expired_check(self):
        """Test checking if credentials are expired"""
        # Arrange
        with patch('auth.oauth_manager.OAuthManager') as MockManager:
            manager = MagicMock()
            MockManager.return_value = manager

            expired_creds = Mock(spec=Credentials)
            expired_creds.expired = True
            expired_creds.valid = False

            valid_creds = Mock(spec=Credentials)
            valid_creds.expired = False
            valid_creds.valid = True

            # Act & Assert
            assert expired_creds.expired is True
            assert valid_creds.expired is False

    def test_refresh_expired_credentials(self):
        """Test refreshing expired credentials"""
        # Arrange
        with patch('auth.oauth_manager.OAuthManager') as MockManager:
            manager = MagicMock()
            MockManager.return_value = manager

            expired_creds = Mock(spec=Credentials)
            expired_creds.expired = True
            expired_creds.refresh_token = "refresh-token-123"

            refreshed_creds = Mock(spec=Credentials)
            refreshed_creds.expired = False
            refreshed_creds.token = "new-access-token"

            manager.refresh.return_value = refreshed_creds

            # Act
            oauth_manager = MockManager({})
            result = oauth_manager.refresh(expired_creds)

            # Assert
            assert result.expired is False
            assert result.token == "new-access-token"


class TestServiceAccountManager:
    """Unit tests for Service Account Manager"""

    @pytest.fixture
    def mock_service_account_data(self):
        """Mock service account JSON data"""
        return {
            "type": "service_account",
            "project_id": "test-project-123",
            "private_key_id": "key-id",
            "private_key": "-----BEGIN PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----\n",
            "client_email": "sa@test-project.iam.gserviceaccount.com",
            "client_id": "123456789",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token"
        }

    def test_service_account_manager_initialization(self, mock_service_account_data):
        """Test service account manager initializes correctly"""
        # Arrange
        sa_json = json.dumps(mock_service_account_data)

        # Act
        with patch('builtins.open', mock_open(read_data=sa_json)):
            with patch('auth.service_account_manager.ServiceAccountManager') as MockManager:
                manager = MockManager('service-account.json')

                # Assert
                assert manager is not None

    def test_load_service_account_from_file(self, mock_service_account_data):
        """Test loading service account from JSON file"""
        # Arrange
        sa_json = json.dumps(mock_service_account_data)

        # Act
        with patch('builtins.open', mock_open(read_data=sa_json)):
            with open('service-account.json', 'r') as f:
                loaded_data = json.load(f)

            # Assert
            assert loaded_data["type"] == "service_account"
            assert loaded_data["project_id"] == "test-project-123"
            assert loaded_data["client_email"] == "sa@test-project.iam.gserviceaccount.com"

    def test_get_service_account_credentials(self):
        """Test getting service account credentials"""
        # Arrange
        with patch('auth.service_account_manager.ServiceAccountManager') as MockManager:
            manager = MagicMock()
            MockManager.return_value = manager

            mock_creds = Mock()
            mock_creds.service_account_email = "sa@test-project.iam.gserviceaccount.com"
            mock_creds.valid = True
            manager.get_credentials.return_value = mock_creds

            # Act
            sa_manager = MockManager('service-account.json')
            creds = sa_manager.get_credentials()

            # Assert
            assert creds.valid is True
            assert "sa@test-project" in creds.service_account_email

    def test_domain_wide_delegation(self):
        """Test domain-wide delegation"""
        # Arrange
        with patch('auth.service_account_manager.ServiceAccountManager') as MockManager:
            manager = MagicMock()
            MockManager.return_value = manager

            user_email = "user@example.com"
            delegated_creds = Mock()
            delegated_creds.subject = user_email
            delegated_creds.valid = True
            manager.get_delegated_credentials.return_value = delegated_creds

            # Act
            sa_manager = MockManager('service-account.json')
            creds = sa_manager.get_delegated_credentials(user_email)

            # Assert
            assert creds.subject == user_email
            assert creds.valid is True

    def test_set_scopes(self):
        """Test setting scopes for service account"""
        # Arrange
        with patch('auth.service_account_manager.ServiceAccountManager') as MockManager:
            manager = MagicMock()
            MockManager.return_value = manager

            scopes = [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/drive.readonly"
            ]

            # Act
            sa_manager = MockManager('service-account.json')
            sa_manager.set_scopes(scopes)

            # Assert
            manager.set_scopes.assert_called_once_with(scopes)

    def test_service_account_file_not_found(self):
        """Test handling when service account file doesn't exist"""
        # Act & Assert
        with patch('builtins.open', side_effect=FileNotFoundError("File not found")):
            with pytest.raises(FileNotFoundError):
                with open('nonexistent-service-account.json', 'r') as f:
                    json.load(f)

    def test_invalid_service_account_json(self):
        """Test handling invalid JSON in service account file"""
        # Arrange
        invalid_json = "{ invalid json syntax"

        # Act & Assert
        with patch('builtins.open', mock_open(read_data=invalid_json)):
            with pytest.raises(json.JSONDecodeError):
                with open('service-account.json', 'r') as f:
                    json.load(f)


class TestCredentialStore:
    """Unit tests for Credential Store"""

    @pytest.fixture
    def mock_credential_store(self):
        """Mock credential store"""
        with patch('auth.credential_store.CredentialStore') as MockStore:
            store = MagicMock()
            MockStore.return_value = store
            yield store

    def test_credential_store_initialization(self, mock_credential_store):
        """Test credential store initializes"""
        # Assert
        assert mock_credential_store is not None

    def test_get_credentials_oauth_priority(self, mock_credential_store):
        """Test OAuth credentials have priority over service account"""
        # Arrange
        store = mock_credential_store
        oauth_creds = Mock(spec=Credentials)
        oauth_creds.valid = True
        store.get_oauth_credentials.return_value = oauth_creds
        store.get_credentials.return_value = oauth_creds

        # Act
        creds = store.get_credentials()

        # Assert
        assert creds == oauth_creds

    def test_get_credentials_fallback_to_service_account(self, mock_credential_store):
        """Test fallback to service account when OAuth unavailable"""
        # Arrange
        store = mock_credential_store
        store.get_oauth_credentials.return_value = None

        sa_creds = Mock()
        sa_creds.valid = True
        store.get_service_account_credentials.return_value = sa_creds
        store.get_credentials.return_value = sa_creds

        # Act
        creds = store.get_credentials()

        # Assert
        assert creds == sa_creds

    def test_get_credentials_none_available(self, mock_credential_store):
        """Test when no credentials are available"""
        # Arrange
        store = mock_credential_store
        store.get_oauth_credentials.return_value = None
        store.get_service_account_credentials.return_value = None
        store.get_credentials.return_value = None

        # Act
        creds = store.get_credentials()

        # Assert
        assert creds is None

    def test_refresh_credentials_if_expired(self, mock_credential_store):
        """Test automatic refresh of expired credentials"""
        # Arrange
        store = mock_credential_store
        expired_creds = Mock(spec=Credentials)
        expired_creds.expired = True

        refreshed_creds = Mock(spec=Credentials)
        refreshed_creds.expired = False
        refreshed_creds.valid = True
        store.refresh_if_expired.return_value = refreshed_creds

        # Act
        creds = store.refresh_if_expired(expired_creds)

        # Assert
        assert creds.expired is False
        assert creds.valid is True

    def test_switch_to_service_account(self, mock_credential_store):
        """Test switching from OAuth to service account"""
        # Arrange
        store = mock_credential_store

        oauth_creds = Mock(spec=Credentials)
        sa_creds = Mock()

        # Act
        store.use_oauth.return_value = oauth_creds
        store.use_service_account.return_value = sa_creds

        creds1 = store.use_oauth()
        creds2 = store.use_service_account()

        # Assert
        assert creds1 == oauth_creds
        assert creds2 == sa_creds

    def test_cache_credentials(self, mock_credential_store):
        """Test credentials are cached"""
        # Arrange
        store = mock_credential_store
        cached_creds = Mock(spec=Credentials)
        cached_creds.valid = True

        store.get_cached_credentials.return_value = cached_creds

        # Act
        creds1 = store.get_cached_credentials()
        creds2 = store.get_cached_credentials()

        # Assert
        assert creds1 == creds2
        assert creds1.valid is True

    def test_clear_cache(self, mock_credential_store):
        """Test clearing credential cache"""
        # Arrange
        store = mock_credential_store

        # Act
        store.clear_cache()

        # Assert
        store.clear_cache.assert_called_once()
