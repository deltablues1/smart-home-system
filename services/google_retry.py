"""Small retry helpers for Google runtime calls in voice paths."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from tools.resilience.retry_handler import RetryConfig, calculate_backoff

logger = logging.getLogger(__name__)

T = TypeVar("T")


def classify_google_runtime_error(exc: Exception) -> tuple[str, bool, bool]:
    """
    Classify an exception for bounded runtime retries.

    Returns:
        (error_type, retryable, is_quota)
    """
    error_text = str(exc)
    upper_error = error_text.upper()

    if "429" in error_text or "RESOURCE_EXHAUSTED" in upper_error:
        return "quota", True, True

    if "DEADLINE_EXCEEDED" in upper_error or "TIMEOUT" in upper_error:
        return "timeout", True, False

    transient_markers = (
        "UNAVAILABLE",
        "503",
        "502",
        "500",
        "TOO MANY REQUESTS",
        "TRY AGAIN",
        "TEMPORARY",
        "NETWORK",
        "CONNECTION RESET",
    )
    if any(marker in upper_error for marker in transient_markers):
        return "transient", True, False

    if "403" in error_text or "PERMISSION_DENIED" in upper_error:
        if "BILLING_DISABLED" in upper_error:
            return "billing", False, False
        return "permission", False, False

    if "401" in error_text or "UNAUTHENTICATED" in upper_error:
        return "auth", False, False

    if "400" in error_text or "INVALID_ARGUMENT" in upper_error:
        return "invalid_argument", False, False

    return type(exc).__name__, False, False


async def run_with_bounded_retry(
    operation_name: str,
    func: Callable[[], Awaitable[T]],
    *,
    config: RetryConfig,
    log: logging.Logger | None = None,
) -> T:
    """Run an async operation with bounded retry/backoff."""
    active_logger = log or logger
    last_exc: Exception | None = None

    for attempt in range(config.max_retries + 1):
        try:
            return await func()
        except Exception as exc:  # pragma: no cover - branches tested through helper
            last_exc = exc
            error_type, retryable, is_quota = classify_google_runtime_error(exc)
            if attempt >= config.max_retries or not retryable:
                raise

            delay = calculate_backoff(attempt, config)
            active_logger.warning(
                "%s failed (%s, quota=%s) attempt %s/%s; retrying in %.2fs",
                operation_name,
                error_type,
                is_quota,
                attempt + 1,
                config.max_retries + 1,
                delay,
            )
            await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc

    raise RuntimeError(f"{operation_name} failed without raising an exception")
