"""
Resilience Layer - Advanced Error Handling & Recovery

Modules:
- retry_handler: Exponential backoff retry logic
- circuit_breaker: Circuit breaker pattern for preventing cascading failures
- rate_limiter: Token bucket rate limiting with per-service and per-user limits
- cache: Intelligent caching with TTL and LRU eviction
"""

from .retry_handler import (
    RetryConfig,
    with_retry,
    is_retryable_error,
    calculate_backoff
)

from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    CircuitOpenError,
    with_circuit_breaker,
    get_circuit_breaker,
    get_all_circuit_states,
    reset_all_circuits,
    get_health_status
)

from .rate_limiter import (
    RateLimiter,
    RateLimitConfig,
    TokenBucket,
    RateLimitExceededError,
    with_rate_limit,
    get_rate_limiter,
    get_service_config,
    get_all_metrics,
    get_service_status,
    reset_rate_limiter,
    get_gmail_cost,
    GMAIL_QUOTA_COSTS,
    SERVICE_CONFIGS
)

from .cache import (
    CacheManager,
    CacheConfig,
    CacheEntry,
    LRUCache,
    with_cache,
    get_cache_manager,
    get_cache_stats,
    invalidate_cache,
    get_cache_config,
    CACHE_CONFIGS
)

__all__ = [
    # Retry Handler
    'RetryConfig',
    'with_retry',
    'is_retryable_error',
    'calculate_backoff',
    # Circuit Breaker
    'CircuitBreaker',
    'CircuitBreakerConfig',
    'CircuitState',
    'CircuitOpenError',
    'with_circuit_breaker',
    'get_circuit_breaker',
    'get_all_circuit_states',
    'reset_all_circuits',
    'get_health_status',
    # Rate Limiter
    'RateLimiter',
    'RateLimitConfig',
    'TokenBucket',
    'RateLimitExceededError',
    'with_rate_limit',
    'get_rate_limiter',
    'get_service_config',
    'get_all_metrics',
    'get_service_status',
    'reset_rate_limiter',
    'get_gmail_cost',
    'GMAIL_QUOTA_COSTS',
    'SERVICE_CONFIGS',
    # Cache
    'CacheManager',
    'CacheConfig',
    'CacheEntry',
    'LRUCache',
    'with_cache',
    'get_cache_manager',
    'get_cache_stats',
    'invalidate_cache',
    'get_cache_config',
    'CACHE_CONFIGS'
]
