"""
Circuit Breaker Pattern for Google APIs

Prevents cascading failures by stopping requests to failing services.
Based on industry best practices and optimized for Google Workspace APIs.

Circuit States:
- CLOSED: Normal operation, all requests allowed
- OPEN: Service failing, all requests blocked (fast-fail)
- HALF_OPEN: Testing recovery, limited requests allowed

State Transitions:
- CLOSED → OPEN: When failure threshold is reached
- OPEN → HALF_OPEN: After timeout period expires
- HALF_OPEN → CLOSED: When success threshold is met
- HALF_OPEN → OPEN: On any failure during recovery test

Features:
- Per-service circuit breakers (Gmail, Drive, Docs, etc.)
- Configurable thresholds and timeouts
- Automatic recovery testing
- Thread-safe async implementation
- Detailed state tracking and logging
"""

import asyncio
import time
from typing import Callable, TypeVar, Any, Optional, Dict
from enum import Enum
from functools import wraps
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

T = TypeVar('T')

# HTTP statuses that indicate a caller problem, not service degradation —
# they must not count toward opening the circuit.
_NON_TRANSIENT_HTTP_STATUSES = {400, 401, 403, 404, 409, 412, 422}


def _is_circuit_relevant_error(error: Exception) -> bool:
    """True when the error signals service trouble (5xx/429/connection/timeout).

    4xx caller errors (auth, validation, not-found) return False.
    Unknown error types conservatively count as failures.
    """
    status = getattr(getattr(error, "resp", None), "status", None)  # googleapiclient HttpError
    if status is None:
        status = getattr(error, "status_code", None)
    if status is not None:
        try:
            status = int(status)
        except (TypeError, ValueError):
            return True
        if status in _NON_TRANSIENT_HTTP_STATUSES:
            return False
        return True
    if isinstance(error, (ValueError, TypeError, KeyError, PermissionError)):
        return False
    return True


class CircuitState(Enum):
    """
    Circuit breaker states

    CLOSED: Normal operation - requests flow through
    OPEN: Failure state - requests are blocked (fast-fail)
    HALF_OPEN: Recovery testing - limited requests allowed
    """
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """
    Configuration for circuit breaker behavior

    Attributes:
        failure_threshold: Number of consecutive failures before opening circuit
        success_threshold: Number of consecutive successes to close circuit from half-open
        timeout: Time (seconds) to wait before transitioning from OPEN to HALF_OPEN
        half_open_max_calls: Maximum concurrent calls allowed in HALF_OPEN state
    """
    failure_threshold: int = 5
    success_threshold: int = 2
    timeout: float = 60.0
    half_open_max_calls: int = 1

    def __post_init__(self):
        """Validate configuration"""
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.success_threshold < 1:
            raise ValueError("success_threshold must be >= 1")
        if self.timeout <= 0:
            raise ValueError("timeout must be > 0")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")


class CircuitOpenError(Exception):
    """
    Exception raised when circuit is OPEN

    This indicates that the service is currently unavailable
    and requests are being blocked to prevent cascading failures.
    """
    pass


class CircuitBreaker:
    """
    Circuit Breaker implementation for API calls

    Protects against cascading failures by monitoring API call success/failure
    rates and automatically blocking requests to failing services.

    Usage:
        breaker = CircuitBreaker("gmail", CircuitBreakerConfig())
        result = await breaker.call(gmail_send_message, credentials, to, subject, body)

    Thread Safety:
        This implementation uses asyncio.Lock for thread-safe operations.
    """

    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        """
        Initialize circuit breaker

        Args:
            name: Service name (e.g., "gmail", "drive", "docs")
            config: Circuit breaker configuration (default: CircuitBreakerConfig())
        """
        self.name = name
        self.config = config or CircuitBreakerConfig()

        # State tracking
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self.half_open_calls = 0

        # Thread safety
        self._lock = asyncio.Lock()

        # Statistics
        self.total_calls = 0
        self.total_failures = 0
        self.total_successes = 0
        self.state_changes = 0

        logger.debug(
            f"Circuit breaker '{name}' initialized: "
            f"failure_threshold={self.config.failure_threshold}, "
            f"timeout={self.config.timeout}s"
        )

    async def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        """
        Execute function through circuit breaker

        Args:
            func: Async function to execute
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func

        Returns:
            Result from func

        Raises:
            CircuitOpenError: If circuit is OPEN and blocking requests
            Exception: Any exception raised by func (if circuit allows execution)
        """
        async with self._lock:
            self.total_calls += 1

            # Check and update state
            await self._check_state()

            # OPEN state - fast fail
            if self.state == CircuitState.OPEN:
                time_until_half_open = self._time_until_half_open()
                logger.warning(
                    f"[WARNING]  Circuit '{self.name}' is OPEN - blocking request. "
                    f"Retry in {time_until_half_open:.1f}s"
                )
                raise CircuitOpenError(
                    f"Circuit '{self.name}' is OPEN (service unavailable). "
                    f"Try again in {time_until_half_open:.1f}s"
                )

            # HALF_OPEN state - limit concurrent calls
            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls >= self.config.half_open_max_calls:
                    logger.debug(
                        f"Circuit '{self.name}' is HALF_OPEN - recovery test in progress"
                    )
                    raise CircuitOpenError(
                        f"Circuit '{self.name}' is HALF_OPEN (testing recovery). "
                        "Please wait for test to complete."
                    )
                self.half_open_calls += 1

        # Execute function (outside lock to allow concurrency)
        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result

        except Exception as e:
            if _is_circuit_relevant_error(e):
                await self._on_failure(e)
            else:
                # Caller errors (401/403/404/validation) say nothing about the
                # service's health — counting them would trip the breaker for
                # every user because one request was malformed or unauthorized.
                logger.debug(
                    f"Circuit '{self.name}': ignoring non-transient error "
                    f"{type(e).__name__}"
                )
            raise

        finally:
            if self.state == CircuitState.HALF_OPEN:
                async with self._lock:
                    self.half_open_calls -= 1

    async def _check_state(self):
        """
        Check and update circuit state

        Transitions OPEN → HALF_OPEN if timeout has expired
        """
        if self.state == CircuitState.OPEN:
            time_until_half_open = self._time_until_half_open()

            if time_until_half_open <= 0:
                logger.info(
                    f"🔄 Circuit '{self.name}': OPEN → HALF_OPEN "
                    "(timeout expired, testing recovery)"
                )
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                self.success_count = 0
                self.failure_count = 0
                self.state_changes += 1

    async def _on_success(self):
        """
        Handle successful function execution

        In CLOSED state: Resets failure count
        In HALF_OPEN state: Increments success count, closes circuit if threshold met
        """
        async with self._lock:
            self.total_successes += 1

            if self.state == CircuitState.CLOSED:
                # Reset failure count on success
                if self.failure_count > 0:
                    logger.debug(
                        f"Circuit '{self.name}' success - resetting failure count "
                        f"(was {self.failure_count})"
                    )
                    self.failure_count = 0

            elif self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                logger.info(
                    f"[OK] Circuit '{self.name}' recovery test success "
                    f"({self.success_count}/{self.config.success_threshold})"
                )

                # Enough successes - close circuit
                if self.success_count >= self.config.success_threshold:
                    logger.info(
                        f"🟢 Circuit '{self.name}': HALF_OPEN → CLOSED "
                        "(recovery successful)"
                    )
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self.success_count = 0
                    self.state_changes += 1

    async def _on_failure(self, error: Exception):
        """
        Handle failed function execution

        In CLOSED state: Increments failure count, opens circuit if threshold met
        In HALF_OPEN state: Immediately opens circuit (recovery failed)

        Args:
            error: Exception that caused the failure
        """
        async with self._lock:
            self.total_failures += 1
            self.last_failure_time = time.time()
            error_type = type(error).__name__

            if self.state == CircuitState.CLOSED:
                self.failure_count += 1
                logger.warning(
                    f"[WARNING]  Circuit '{self.name}' failure "
                    f"({self.failure_count}/{self.config.failure_threshold}): "
                    f"{error_type}"
                )

                # Too many failures - open circuit
                if self.failure_count >= self.config.failure_threshold:
                    logger.error(
                        f"[CIRCUIT OPEN] Circuit '{self.name}': CLOSED → OPEN "
                        f"(failure threshold reached: {self.config.failure_threshold})"
                    )
                    self.state = CircuitState.OPEN
                    self.state_changes += 1

            elif self.state == CircuitState.HALF_OPEN:
                # Any failure in half-open - back to open
                logger.error(
                    f"[CIRCUIT OPEN] Circuit '{self.name}': HALF_OPEN → OPEN "
                    f"(recovery test failed: {error_type})"
                )
                self.state = CircuitState.OPEN
                self.success_count = 0
                self.failure_count = self.config.failure_threshold  # Max to prevent immediate retry
                self.state_changes += 1

    def _time_until_half_open(self) -> float:
        """
        Calculate time remaining until HALF_OPEN transition

        Returns:
            Seconds until transition (0 if ready to transition)
        """
        if not self.last_failure_time:
            return 0

        elapsed = time.time() - self.last_failure_time
        return max(0, self.config.timeout - elapsed)

    def get_state(self) -> Dict[str, Any]:
        """
        Get current circuit breaker state and statistics

        Returns:
            Dictionary with state information
        """
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "success_rate": (
                f"{(self.total_successes / self.total_calls * 100):.1f}%"
                if self.total_calls > 0 else "N/A"
            ),
            "state_changes": self.state_changes,
            "time_until_half_open": (
                self._time_until_half_open()
                if self.state == CircuitState.OPEN else None
            )
        }

    async def reset(self):
        """
        Manually reset circuit breaker to CLOSED state

        Use with caution - typically only for testing or manual intervention
        """
        async with self._lock:
            logger.warning(f"[WARNING]  Circuit '{self.name}' manually reset to CLOSED")
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.half_open_calls = 0


# Global registry of circuit breakers (one per service)
_circuit_breakers: Dict[str, CircuitBreaker] = {}
_registry_lock = asyncio.Lock()


async def get_circuit_breaker(
    service_name: str,
    config: Optional[CircuitBreakerConfig] = None
) -> CircuitBreaker:
    """
    Get or create circuit breaker for a service

    Circuit breakers are singleton per service (shared across all calls)

    Args:
        service_name: Service name (e.g., "gmail", "drive", "docs")
        config: Configuration (used only when creating new breaker)

    Returns:
        CircuitBreaker instance for the service

    Example:
        breaker = await get_circuit_breaker("gmail")
        result = await breaker.call(gmail_function, ...)
    """
    async with _registry_lock:
        if service_name not in _circuit_breakers:
            if config is None:
                config = CircuitBreakerConfig()
            _circuit_breakers[service_name] = CircuitBreaker(service_name, config)
            logger.info(f"Created new circuit breaker for service: {service_name}")

        return _circuit_breakers[service_name]


def with_circuit_breaker(
    service_name: str,
    config: Optional[CircuitBreakerConfig] = None
):
    """
    Decorator for circuit breaker protection

    Wraps async functions to automatically apply circuit breaker pattern.

    Args:
        service_name: Service name (e.g., "gmail", "drive", "docs")
        config: Circuit breaker configuration (optional)

    Returns:
        Decorated function with circuit breaker protection

    Example:
        @with_circuit_breaker("gmail")
        @with_retry(RetryConfig(max_retries=3))
        async def gmail_send_message(credentials, to, subject, body):
            # API call
            ...

    Note:
        When combining with @with_retry, circuit breaker should be OUTER decorator:

        CORRECT order:
            @with_circuit_breaker(...)  # Outer
            @with_retry(...)            # Inner
            async def my_function():
                ...

        This ensures retries happen BEFORE circuit opens.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            breaker = await get_circuit_breaker(service_name, config)
            return await breaker.call(func, *args, **kwargs)
        return wrapper
    return decorator


async def get_all_circuit_states() -> Dict[str, Dict[str, Any]]:
    """
    Get state of all circuit breakers

    Useful for monitoring and health checks

    Returns:
        Dictionary mapping service name to state info

    Example:
        states = await get_all_circuit_states()
        for service, state in states.items():
            print(f"{service}: {state['state']} ({state['success_rate']})")
    """
    async with _registry_lock:
        return {
            name: breaker.get_state()
            for name, breaker in _circuit_breakers.items()
        }


async def reset_all_circuits():
    """
    Reset all circuit breakers to CLOSED state

    Use with caution - typically only for testing or recovery scenarios
    """
    async with _registry_lock:
        for breaker in _circuit_breakers.values():
            await breaker.reset()
        logger.warning("[WARNING]  All circuit breakers reset to CLOSED")


# Convenience function for health checks
async def get_health_status() -> Dict[str, Any]:
    """
    Get health status of all services based on circuit breaker states

    Returns:
        Dictionary with overall health and per-service status

    Example:
        health = await get_health_status()
        if health["overall_healthy"]:
            print("All services operational")
        else:
            print(f"Services down: {health['unhealthy_services']}")
    """
    states = await get_all_circuit_states()

    if not states:
        return {
            "overall_healthy": True,
            "services": {},
            "unhealthy_services": []
        }

    unhealthy = [
        name for name, state in states.items()
        if state["state"] != "closed"
    ]

    return {
        "overall_healthy": len(unhealthy) == 0,
        "total_services": len(states),
        "healthy_services": len(states) - len(unhealthy),
        "unhealthy_services": unhealthy,
        "services": states
    }
