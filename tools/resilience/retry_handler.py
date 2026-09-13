"""
Retry Handler with Exponential Backoff

Automatic retry logic for transient API failures with intelligent backoff strategy.

Features:
- Exponential backoff with configurable base delay
- Jitter to prevent thundering herd
- Transient vs permanent error detection
- Async/await support
- Detailed logging
"""

import asyncio
import random
import time
from typing import Callable, TypeVar, Any, Optional
from functools import wraps
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

T = TypeVar('T')


@dataclass
class RetryConfig:
    """
    Configuration for retry logic

    Attributes:
        max_retries: Maximum number of retry attempts (default: 3)
        base_delay: Base delay in seconds before first retry (default: 1.0)
        max_delay: Maximum delay in seconds between retries (default: 60.0)
        exponential_base: Base for exponential backoff calculation (default: 2.0)
        jitter: Add random jitter to prevent thundering herd (default: True)
    """
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True

    def __post_init__(self):
        """Validate configuration"""
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.base_delay <= 0:
            raise ValueError("base_delay must be > 0")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be >= base_delay")
        if self.exponential_base <= 1:
            raise ValueError("exponential_base must be > 1")


def calculate_backoff(attempt: int, config: RetryConfig) -> float:
    """
    Calculate backoff time with exponential growth and optional jitter

    Formula: delay = min(base_delay * (exponential_base ** attempt), max_delay)
    Jitter: delay += random(0, delay * 0.1)

    Args:
        attempt: Current retry attempt number (0-indexed)
        config: Retry configuration

    Returns:
        Delay in seconds before next retry

    Example:
        >>> config = RetryConfig(base_delay=1.0, exponential_base=2.0)
        >>> calculate_backoff(0, config)  # First retry
        1.0  # (± jitter)
        >>> calculate_backoff(1, config)  # Second retry
        2.0  # (± jitter)
        >>> calculate_backoff(2, config)  # Third retry
        4.0  # (± jitter)
    """
    # Exponential backoff
    delay = min(
        config.base_delay * (config.exponential_base ** attempt),
        config.max_delay
    )

    # Add jitter to prevent thundering herd
    if config.jitter:
        # Add ±10% random variation
        jitter_amount = delay * 0.1 * random.random()
        delay += jitter_amount

    return delay


def is_retryable_error(error: Exception) -> bool:
    """
    Determine if an error is transient (retryable) or permanent

    Transient errors (retryable):
    - HTTP 429 (Rate Limit)
    - HTTP 500, 502, 503, 504 (Server errors)
    - Timeout errors
    - Network/connection errors

    Permanent errors (not retryable):
    - HTTP 400 (Bad Request)
    - HTTP 401 (Unauthorized)
    - HTTP 403 (Forbidden)
    - HTTP 404 (Not Found)
    - Validation errors

    Args:
        error: Exception to check

    Returns:
        True if error is transient and should be retried, False otherwise

    Example:
        >>> from googleapiclient.errors import HttpError
        >>> # Mock 429 error
        >>> is_retryable_error(http_429_error)
        True
        >>> # Mock 404 error
        >>> is_retryable_error(http_404_error)
        False
    """
    # HttpError - check status code
    try:
        from googleapiclient.errors import HttpError

        if isinstance(error, HttpError):
            status = error.resp.status

            # Retryable HTTP status codes
            retryable_statuses = {429, 500, 502, 503, 504}
            if status in retryable_statuses:
                logger.debug(f"Retryable HTTP error: {status}")
                return True

            # Permanent HTTP status codes
            permanent_statuses = {400, 401, 403, 404}
            if status in permanent_statuses:
                logger.debug(f"Permanent HTTP error: {status}")
                return False

    except ImportError:
        # googleapiclient not available
        pass

    # Timeout errors - retryable
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        logger.debug("Timeout error - retryable")
        return True

    # Network/connection errors - check error message
    error_str = str(error).lower()
    transient_keywords = [
        'timeout',
        'connection',
        'network',
        'temporary',
        'unavailable',
        'try again',
        'rate limit'
    ]

    if any(keyword in error_str for keyword in transient_keywords):
        logger.debug(f"Transient error detected: {error_str[:100]}")
        return True

    # Default: not retryable for unknown errors
    logger.debug(f"Permanent error (unknown type): {type(error).__name__}")
    return False


def with_retry(config: Optional[RetryConfig] = None):
    """
    Decorator for automatic retry with exponential backoff

    Wraps async functions to automatically retry on transient failures.
    Only retries errors identified as transient by is_retryable_error().

    Args:
        config: Retry configuration (default: RetryConfig())

    Returns:
        Decorated function with retry logic

    Example:
        >>> @with_retry(RetryConfig(max_retries=5, base_delay=2.0))
        ... async def my_api_call(credentials, document_id: str):
        ...     # API call that might fail transiently
        ...     return await api.get_document(document_id)

        >>> # Call with automatic retry
        >>> result = await my_api_call(creds, "doc123")

    Raises:
        Exception: Re-raises the last exception if all retries are exhausted
    """
    if config is None:
        config = RetryConfig()

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_error: Optional[Exception] = None
            func_name = func.__name__

            # Attempt original call + retries
            for attempt in range(config.max_retries + 1):
                try:
                    # Execute function
                    if attempt > 0:
                        logger.info(
                            f"🔄 Retry attempt {attempt}/{config.max_retries} for {func_name}"
                        )

                    result = await func(*args, **kwargs)

                    # Success!
                    if attempt > 0:
                        logger.info(
                            f"[OK] Retry successful for {func_name} after {attempt} attempt(s)"
                        )

                    return result

                except Exception as e:
                    last_error = e

                    # Check if error is retryable
                    if not is_retryable_error(e):
                        logger.error(
                            f"[ERROR] Permanent error in {func_name}: {type(e).__name__}: {str(e)[:200]}"
                        )
                        raise

                    # Last attempt reached - don't retry anymore
                    if attempt == config.max_retries:
                        logger.error(
                            f"[ERROR] Max retries ({config.max_retries}) exhausted for {func_name}: "
                            f"{type(e).__name__}: {str(e)[:200]}"
                        )
                        raise

                    # Calculate backoff delay
                    delay = calculate_backoff(attempt, config)

                    logger.warning(
                        f"[WARNING]  Transient error in {func_name} "
                        f"(attempt {attempt + 1}/{config.max_retries + 1}): "
                        f"{type(e).__name__}: {str(e)[:100]}. "
                        f"Retrying in {delay:.2f}s..."
                    )

                    # Wait before retry
                    await asyncio.sleep(delay)

            # This should not be reached, but just in case
            if last_error:
                raise last_error

            # Fallback (should never happen)
            raise RuntimeError(f"Unexpected error in retry logic for {func_name}")

        return wrapper
    return decorator


class UnconfirmedWrite(Exception):
    """A write whose outcome is unknown: it may or may not have been applied."""


def report_unconfirmed(what: str):
    """Mark a NON-IDEMPOTENT write so a lost answer is reported, not repeated.

    These operations deliberately have no @with_retry: retrying a create or an
    append after a 504 or a client timeout duplicates it, because the write may
    already have been applied and only the response is gone. Same reasoning as
    the missing retry on gmail_send_message and calendar_create_event.

    Dropping the retry alone is not enough, though. The agent reads a raw
    "connection reset" as a plain failure and calls the tool again itself, so
    the duplicate comes back one level up. The transient errors that used to
    trigger a retry are therefore surfaced as an explicit "not confirmed",
    which the ADK wrappers return as status="unknown" rather than "failed".

    Permanent errors (400/401/403/404) pass through unchanged: those really
    did not happen.

    **What this does and does not do.** It removes the automatic retry and it
    keeps the unknown outcome structured all the way to the model. It does
    NOT programmatically stop the agent from calling the tool a second time —
    nothing here knows that the two calls are the same operation. That needs
    an operation identity and a recorded pending/completed state, which
    belongs with the step-result contract, not here. Until then this is a
    clear signal, not a guard.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except UnconfirmedWrite:
                raise
            except Exception as e:
                if not is_retryable_error(e):
                    raise
                logger.warning(
                    "%s: transient error, outcome unknown: %s", func.__name__, e
                )
                raise UnconfirmedWrite(
                    f"{what}: NIJE POTVRĐENO je li izvršeno — veza je pukla nakon "
                    f"slanja zahtjeva. Provjeri trenutno stanje prije nego "
                    f"ponoviš, jer bi ponavljanje moglo napraviti duplikat. "
                    f"(uzrok: {e})"
                ) from e

        return wrapper
    return decorator


# Convenience functions for common retry configurations

def with_aggressive_retry():
    """
    Aggressive retry configuration for critical operations

    Config:
    - 5 retries
    - 0.5s base delay
    - Max 30s delay
    """
    return with_retry(RetryConfig(
        max_retries=5,
        base_delay=0.5,
        max_delay=30.0
    ))


def with_conservative_retry():
    """
    Conservative retry configuration for less critical operations

    Config:
    - 2 retries
    - 2s base delay
    - Max 60s delay
    """
    return with_retry(RetryConfig(
        max_retries=2,
        base_delay=2.0,
        max_delay=60.0
    ))


def with_standard_retry():
    """
    Standard retry configuration (default)

    Config:
    - 3 retries
    - 1s base delay
    - Max 60s delay
    """
    return with_retry(RetryConfig(
        max_retries=3,
        base_delay=1.0,
        max_delay=60.0
    ))


def with_quota_retry():
    """
    API quota retry configuration for 429 RESOURCE_EXHAUSTED errors

    Specifically designed for Google API quota limits with longer delays.

    Config:
    - 3 retries (enough for quota to replenish)
    - 15s base delay (allows quota to reset)
    - Max 90s delay
    - Exponential backoff: 15s → 30s → 60s

    Use this for:
    - Google Calendar API calls
    - Google Drive API calls
    - Any API with strict quota limits
    """
    return with_retry(RetryConfig(
        max_retries=3,
        base_delay=15.0,  # Start with 15s
        max_delay=90.0,   # Cap at 90s
        exponential_base=2.0
    ))


# Statistics tracking (optional)
class RetryStats:
    """
    Global retry statistics tracker

    Tracks:
    - Total retry attempts
    - Successful retries
    - Failed retries
    - Per-function retry counts
    """

    def __init__(self):
        self.total_attempts = 0
        self.successful_retries = 0
        self.failed_retries = 0
        self.function_stats = {}

    def record_retry_attempt(self, func_name: str):
        """Record a retry attempt"""
        self.total_attempts += 1
        if func_name not in self.function_stats:
            self.function_stats[func_name] = {'attempts': 0, 'successes': 0, 'failures': 0}
        self.function_stats[func_name]['attempts'] += 1

    def record_retry_success(self, func_name: str):
        """Record a successful retry"""
        self.successful_retries += 1
        if func_name in self.function_stats:
            self.function_stats[func_name]['successes'] += 1

    def record_retry_failure(self, func_name: str):
        """Record a failed retry (exhausted)"""
        self.failed_retries += 1
        if func_name in self.function_stats:
            self.function_stats[func_name]['failures'] += 1

    def get_stats(self):
        """Get retry statistics"""
        success_rate = (
            (self.successful_retries / self.total_attempts * 100)
            if self.total_attempts > 0 else 0
        )

        return {
            'total_attempts': self.total_attempts,
            'successful_retries': self.successful_retries,
            'failed_retries': self.failed_retries,
            'success_rate': f"{success_rate:.1f}%",
            'function_stats': self.function_stats
        }


# Global stats instance
_retry_stats = RetryStats()


def get_retry_stats() -> RetryStats:
    """Get global retry statistics"""
    return _retry_stats
