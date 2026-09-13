"""
Error Classification System

Classifies exceptions into categories and severities to determine
handling strategies (retry, fallback, fail).
"""

from enum import Enum
from typing import Optional, Type, Dict, Any
import google.api_core.exceptions
import google.auth.exceptions
import requests.exceptions

class ErrorSeverity(Enum):
    """Severity of the error"""
    TRANSIENT = "transient"      # Temporary issue, safe to retry (e.g., 429, 503)
    PERMANENT = "permanent"      # Permanent issue, do not retry (e.g., 400, 404)
    CRITICAL = "critical"        # System-level failure, requires immediate attention
    WARNING = "warning"          # Minor issue, can proceed with degraded functionality

class ErrorCategory(Enum):
    """Category of the error"""
    API_ERROR = "api_error"           # External API failure
    AUTHENTICATION = "authentication" # Auth failure
    NETWORK = "network"               # Connectivity issue
    VALIDATION = "validation"         # Invalid input/data
    RESOURCE = "resource"             # Resource not found/exhausted
    SYSTEM = "system"                 # Internal system error
    UNKNOWN = "unknown"

class ErrorClassification:
    """Result of error classification"""
    def __init__(
        self,
        severity: ErrorSeverity,
        category: ErrorCategory,
        retryable: bool,
        user_message_key: str
    ):
        self.severity = severity
        self.category = category
        self.retryable = retryable
        self.user_message_key = user_message_key

def classify_error(exception: Exception) -> ErrorClassification:
    """
    Classifies an exception into severity and category.
    
    Args:
        exception: The exception to classify
        
    Returns:
        ErrorClassification object
    """
    
    # Google API Errors
    if isinstance(exception, google.api_core.exceptions.GoogleAPICallError):
        if isinstance(exception, google.api_core.exceptions.ResourceExhausted): # 429
            return ErrorClassification(
                ErrorSeverity.TRANSIENT,
                ErrorCategory.RESOURCE,
                retryable=True,
                user_message_key="quota_exceeded"
            )
        elif isinstance(exception, google.api_core.exceptions.ServiceUnavailable): # 503
            return ErrorClassification(
                ErrorSeverity.TRANSIENT,
                ErrorCategory.API_ERROR,
                retryable=True,
                user_message_key="service_unavailable"
            )
        elif isinstance(exception, google.api_core.exceptions.NotFound): # 404
            return ErrorClassification(
                ErrorSeverity.PERMANENT,
                ErrorCategory.RESOURCE,
                retryable=False,
                user_message_key="not_found"
            )
        elif isinstance(exception, google.api_core.exceptions.Unauthenticated): # 401
            return ErrorClassification(
                ErrorSeverity.PERMANENT,
                ErrorCategory.AUTHENTICATION,
                retryable=False,
                user_message_key="unauthenticated"
            )
        elif isinstance(exception, google.api_core.exceptions.PermissionDenied): # 403
            return ErrorClassification(
                ErrorSeverity.PERMANENT,
                ErrorCategory.AUTHENTICATION,
                retryable=False,
                user_message_key="permission_denied"
            )
        elif isinstance(exception, google.api_core.exceptions.InvalidArgument): # 400
            return ErrorClassification(
                ErrorSeverity.PERMANENT,
                ErrorCategory.VALIDATION,
                retryable=False,
                user_message_key="invalid_argument"
            )
            
    # Google Auth Errors
    if isinstance(exception, google.auth.exceptions.GoogleAuthError):
        return ErrorClassification(
            ErrorSeverity.PERMANENT,
            ErrorCategory.AUTHENTICATION,
            retryable=False,
            user_message_key="auth_error"
        )

    # Requests/Network Errors
    if isinstance(exception, requests.exceptions.RequestException):
        if isinstance(exception, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return ErrorClassification(
                ErrorSeverity.TRANSIENT,
                ErrorCategory.NETWORK,
                retryable=True,
                user_message_key="network_error"
            )
        
    # Default / Unknown
    return ErrorClassification(
        ErrorSeverity.CRITICAL,
        ErrorCategory.UNKNOWN,
        retryable=False,
        user_message_key="unknown_error"
    )
