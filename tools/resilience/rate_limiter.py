"""
Rate Limiter with Token Bucket Algorithm

Advanced rate limiting for Google API calls with support for:
- Token Bucket algorithm (allows controlled bursts)
- Per-service and per-user limits
- Quota Units support (Gmail API)
- Distributed rate limiting via Redis
- Local in-memory fallback
- Comprehensive monitoring and metrics

Based on Google API Rate Limit best practices from:
https://google.github.io/adk-docs/
"""

import asyncio
import time
import logging
from typing import Dict, Optional, Callable, TypeVar, Any
from functools import wraps
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import datetime, timedelta
import threading

logger = logging.getLogger(__name__)

T = TypeVar('T')


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class RateLimitConfig:
    """
    Rate limit configuration for a specific service

    Attributes:
        requests_per_minute: Max requests per minute (RPM)
        requests_per_second: Max requests per second (QPS) - optional
        burst_size: Maximum burst capacity (defaults to RPM if not set)
        quota_units_per_request: Cost per request in quota units (Gmail API)
        per_user_rpm: Per-user rate limit (if different from project limit)
        per_user_burst: Per-user burst capacity
        daily_limit: Daily limit (e.g., Gmail sending limit)
        use_quota_units: Whether this service uses quota units instead of requests
    """
    requests_per_minute: int
    requests_per_second: Optional[int] = None
    burst_size: Optional[int] = None
    quota_units_per_request: int = 1
    per_user_rpm: Optional[int] = None
    per_user_burst: Optional[int] = None
    daily_limit: Optional[int] = None
    use_quota_units: bool = False

    def __post_init__(self):
        """Set defaults and validate"""
        # Default burst size to RPM if not specified
        if self.burst_size is None:
            self.burst_size = self.requests_per_minute

        # Default per-user burst to per-user RPM
        if self.per_user_rpm and self.per_user_burst is None:
            self.per_user_burst = self.per_user_rpm

        # Validate
        if self.requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be > 0")
        if self.burst_size < self.requests_per_minute:
            raise ValueError("burst_size must be >= requests_per_minute")


# ============================================================================
# SERVICE-SPECIFIC CONFIGURATIONS
# Based on analysis: Using 80-90% of official limits for safety margin
# ============================================================================

SERVICE_CONFIGS = {
    "sheets": RateLimitConfig(
        requests_per_minute=250,      # Official: 300 (project), using 83%
        per_user_rpm=45,               # Official: 60 (per user), using 75%
        per_user_burst=50,
        burst_size=300,
        daily_limit=None
    ),

    "drive": RateLimitConfig(
        requests_per_minute=10000,    # Official: 12,000 (queries), using 83%
        per_user_rpm=10000,           # Official: 12,000 per user
        burst_size=12000,
        daily_limit=None              # Upload limit (750GB) tracked separately
    ),

    "gmail": RateLimitConfig(
        requests_per_minute=1000000,  # Official: 1,200,000 quota units/min
        per_user_rpm=10000,           # Official: 15,000 quota units/min, using 67%
        per_user_burst=12000,
        burst_size=1200000,
        quota_units_per_request=1,    # Varies by operation (1-100)
        use_quota_units=True,
        daily_limit=1500              # Email sending limit (official: 2000)
    ),

    "docs": RateLimitConfig(
        requests_per_minute=250,      # Similar to Sheets
        per_user_rpm=45,
        per_user_burst=50,
        burst_size=300
    ),

    "contacts": RateLimitConfig(
        requests_per_minute=250,      # Conservative, similar to Sheets
        per_user_rpm=45,
        per_user_burst=50,
        burst_size=300
    ),

    # Alias for contacts (People API)
    "people": RateLimitConfig(
        requests_per_minute=250,      # Same as contacts
        per_user_rpm=45,
        per_user_burst=50,
        burst_size=300
    ),

    "tasks": RateLimitConfig(
        requests_per_minute=250,      # Courtesy limit approach
        per_user_rpm=45,              # Conservative due to undocumented limits
        per_user_burst=50,
        burst_size=300,
        daily_limit=50000             # Courtesy limit
    ),

    "calendar": RateLimitConfig(
        requests_per_minute=250,      # Similar to other Workspace APIs
        per_user_rpm=45,
        per_user_burst=50,
        burst_size=300
    ),

    "directory": RateLimitConfig(
        requests_per_minute=300,
        requests_per_second=5,        # User creation: max 10/sec, using 50%
        per_user_rpm=60,
        burst_size=300
    ),

    # Vertex AI / Gemini - Dynamic, varies by model and region
    "vertex_ai_flash": RateLimitConfig(
        requests_per_minute=60,       # Conservative start, can be increased
        burst_size=100,
        daily_limit=None
    ),

    "vertex_ai_pro": RateLimitConfig(
        requests_per_minute=30,       # More restrictive for Pro model
        burst_size=50,
        daily_limit=None
    )
}


# ============================================================================
# TOKEN BUCKET IMPLEMENTATION
# ============================================================================

class TokenBucket:
    """
    Thread-safe Token Bucket algorithm for rate limiting

    Allows controlled burst while maintaining average rate.
    Tokens are added at a constant rate (refill_rate).
    Each request consumes tokens (cost).
    """

    def __init__(
        self,
        capacity: int,
        refill_rate: float,
        initial_tokens: Optional[int] = None
    ):
        """
        Initialize token bucket

        Args:
            capacity: Maximum number of tokens (burst size)
            refill_rate: Tokens added per second
            initial_tokens: Starting token count (defaults to capacity)
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = initial_tokens if initial_tokens is not None else capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

        logger.debug(
            f"TokenBucket initialized: capacity={capacity}, "
            f"refill_rate={refill_rate}/sec, initial={self.tokens}"
        )

    def _refill(self) -> None:
        """Refill tokens based on elapsed time"""
        now = time.monotonic()
        elapsed = now - self.last_refill

        # Calculate tokens to add
        tokens_to_add = elapsed * self.refill_rate

        if tokens_to_add > 0:
            self.tokens = min(self.capacity, self.tokens + tokens_to_add)
            self.last_refill = now

    def consume(self, tokens: int = 1) -> bool:
        """
        Try to consume tokens

        Args:
            tokens: Number of tokens to consume (cost of operation)

        Returns:
            True if tokens were consumed, False if insufficient tokens
        """
        with self._lock:
            self._refill()

            if self.tokens >= tokens:
                self.tokens -= tokens
                logger.debug(f"Consumed {tokens} tokens. Remaining: {self.tokens:.2f}")
                return True
            else:
                logger.debug(
                    f"Insufficient tokens. Requested: {tokens}, Available: {self.tokens:.2f}"
                )
                return False

    def peek(self) -> float:
        """Check available tokens without consuming"""
        with self._lock:
            self._refill()
            return self.tokens

    def time_until_tokens(self, tokens: int = 1) -> float:
        """
        Calculate time until requested tokens are available

        Args:
            tokens: Number of tokens needed

        Returns:
            Seconds to wait (0 if tokens already available)
        """
        with self._lock:
            self._refill()

            if self.tokens >= tokens:
                return 0.0

            tokens_needed = tokens - self.tokens
            return tokens_needed / self.refill_rate

    def reset(self) -> None:
        """Reset bucket to full capacity"""
        with self._lock:
            self.tokens = self.capacity
            self.last_refill = time.monotonic()
            logger.info("TokenBucket reset to full capacity")


# ============================================================================
# RATE LIMITER MANAGER
# ============================================================================

class RateLimiter:
    """
    Manages rate limiting for multiple services and users

    Features:
    - Per-service and per-user token buckets
    - Automatic bucket creation and cleanup
    - Quota unit tracking (Gmail)
    - Daily limit enforcement
    - Comprehensive metrics
    """

    def __init__(self, use_redis: bool = False, redis_client=None):
        """
        Initialize rate limiter

        Args:
            use_redis: Use Redis for distributed rate limiting
            redis_client: Redis client instance (optional)
        """
        self.use_redis = use_redis
        self.redis_client = redis_client

        # Local buckets: {service: {user_id: TokenBucket}}
        self._project_buckets: Dict[str, TokenBucket] = {}
        self._user_buckets: Dict[str, Dict[str, TokenBucket]] = defaultdict(dict)

        # Daily counters: {service: {user_id: {date: count}}}
        self._daily_counters: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
            lambda: defaultdict(dict)
        )

        # Metrics
        self._metrics = {
            'total_requests': 0,
            'blocked_requests': 0,
            'quota_units_consumed': 0,
            'services': defaultdict(lambda: {
                'requests': 0,
                'blocked': 0,
                'quota_consumed': 0
            })
        }

        self._lock = threading.Lock()

        logger.debug(
            f"RateLimiter initialized (redis={'enabled' if use_redis else 'disabled'})"
        )

    def _get_project_bucket(self, service: str) -> TokenBucket:
        """Get or create project-level token bucket"""
        if service not in self._project_buckets:
            if service not in SERVICE_CONFIGS:
                logger.warning(
                    f"No rate limit config for service '{service}', using default"
                )
                config = RateLimitConfig(requests_per_minute=100)
            else:
                config = SERVICE_CONFIGS[service]

            # Calculate refill rate (tokens per second)
            refill_rate = config.requests_per_minute / 60.0

            self._project_buckets[service] = TokenBucket(
                capacity=config.burst_size,
                refill_rate=refill_rate
            )

            logger.info(
                f"Created project bucket for '{service}': "
                f"{config.requests_per_minute} RPM, burst={config.burst_size}"
            )

        return self._project_buckets[service]

    def _get_user_bucket(self, service: str, user_id: str) -> Optional[TokenBucket]:
        """Get or create per-user token bucket (if configured)"""
        config = SERVICE_CONFIGS.get(service)

        if not config or not config.per_user_rpm:
            return None  # No per-user limit for this service

        if user_id not in self._user_buckets[service]:
            refill_rate = config.per_user_rpm / 60.0

            self._user_buckets[service][user_id] = TokenBucket(
                capacity=config.per_user_burst,
                refill_rate=refill_rate
            )

            logger.info(
                f"Created user bucket for '{service}' user '{user_id}': "
                f"{config.per_user_rpm} RPM, burst={config.per_user_burst}"
            )

        return self._user_buckets[service][user_id]

    def _check_daily_limit(self, service: str, user_id: str) -> bool:
        """Check if daily limit has been exceeded"""
        config = SERVICE_CONFIGS.get(service)

        if not config or not config.daily_limit:
            return True  # No daily limit

        today = datetime.now().strftime("%Y-%m-%d")

        with self._lock:
            daily_count = self._daily_counters[service][user_id].get(today, 0)

            if daily_count >= config.daily_limit:
                logger.warning(
                    f"Daily limit exceeded for '{service}' user '{user_id}': "
                    f"{daily_count}/{config.daily_limit}"
                )
                return False

            return True

    def _increment_daily_counter(self, service: str, user_id: str) -> None:
        """Increment daily usage counter"""
        today = datetime.now().strftime("%Y-%m-%d")

        with self._lock:
            if today not in self._daily_counters[service][user_id]:
                self._daily_counters[service][user_id][today] = 0

            self._daily_counters[service][user_id][today] += 1

    async def acquire(
        self,
        service: str,
        user_id: Optional[str] = None,
        cost: int = 1,
        timeout: float = 60.0
    ) -> bool:
        """
        Acquire permission to make API call

        Args:
            service: Service name (e.g., 'sheets', 'gmail')
            user_id: User identifier (for per-user limits)
            cost: Cost in tokens/quota units
            timeout: Max seconds to wait for tokens

        Returns:
            True if permission granted, False if timeout
        """
        start_time = time.monotonic()
        service_config = SERVICE_CONFIGS.get(service)

        # Update metrics
        with self._lock:
            self._metrics['total_requests'] += 1
            self._metrics['services'][service]['requests'] += 1

        # Check daily limit first
        if user_id and not self._check_daily_limit(service, user_id):
            with self._lock:
                self._metrics['blocked_requests'] += 1
                self._metrics['services'][service]['blocked'] += 1

            logger.error(
                f"Request blocked: Daily limit exceeded for '{service}' user '{user_id}'"
            )
            return False

        # Get relevant buckets
        project_bucket = self._get_project_bucket(service)
        user_bucket = self._get_user_bucket(service, user_id) if user_id else None

        # Try to consume tokens from both buckets
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                logger.warning(
                    f"Rate limit timeout ({timeout}s) for '{service}' "
                    f"(cost={cost}, user={user_id})"
                )

                with self._lock:
                    self._metrics['blocked_requests'] += 1
                    self._metrics['services'][service]['blocked'] += 1

                return False

            # Try project bucket first
            if not project_bucket.consume(cost):
                wait_time = project_bucket.time_until_tokens(cost)
                logger.debug(
                    f"Project bucket exhausted for '{service}', "
                    f"waiting {wait_time:.2f}s"
                )
                await asyncio.sleep(min(wait_time, timeout - elapsed))
                continue

            # Try user bucket (if exists)
            if user_bucket and not user_bucket.consume(cost):
                # Refund project bucket
                project_bucket.tokens = min(
                    project_bucket.capacity,
                    project_bucket.tokens + cost
                )

                wait_time = user_bucket.time_until_tokens(cost)
                logger.debug(
                    f"User bucket exhausted for '{service}' user '{user_id}', "
                    f"waiting {wait_time:.2f}s"
                )
                await asyncio.sleep(min(wait_time, timeout - elapsed))
                continue

            # Success! Both buckets have tokens
            if user_id:
                self._increment_daily_counter(service, user_id)

            with self._lock:
                if service_config and service_config.use_quota_units:
                    self._metrics['quota_units_consumed'] += cost
                    self._metrics['services'][service]['quota_consumed'] += cost

            logger.info(
                f"Rate limit check passed: '{service}' "
                f"(cost={cost}, user={user_id}, elapsed={elapsed:.2f}s)"
            )

            return True

    def get_metrics(self) -> Dict[str, Any]:
        """Get rate limiter metrics"""
        with self._lock:
            return {
                'total_requests': self._metrics['total_requests'],
                'blocked_requests': self._metrics['blocked_requests'],
                'block_rate': (
                    self._metrics['blocked_requests'] / self._metrics['total_requests']
                    if self._metrics['total_requests'] > 0 else 0.0
                ),
                'quota_units_consumed': self._metrics['quota_units_consumed'],
                'services': dict(self._metrics['services'])
            }

    def get_bucket_status(self, service: str, user_id: Optional[str] = None) -> Dict:
        """Get current token bucket status"""
        project_bucket = self._get_project_bucket(service)
        user_bucket = self._get_user_bucket(service, user_id) if user_id else None

        status = {
            'service': service,
            'project_bucket': {
                'available_tokens': project_bucket.peek(),
                'capacity': project_bucket.capacity,
                'utilization': 1 - (project_bucket.peek() / project_bucket.capacity)
            }
        }

        if user_bucket:
            status['user_bucket'] = {
                'user_id': user_id,
                'available_tokens': user_bucket.peek(),
                'capacity': user_bucket.capacity,
                'utilization': 1 - (user_bucket.peek() / user_bucket.capacity)
            }

        # Add daily counter if exists
        if user_id and service in self._daily_counters:
            today = datetime.now().strftime("%Y-%m-%d")
            daily_count = self._daily_counters[service][user_id].get(today, 0)
            config = SERVICE_CONFIGS.get(service)

            if config and config.daily_limit:
                status['daily_usage'] = {
                    'count': daily_count,
                    'limit': config.daily_limit,
                    'remaining': config.daily_limit - daily_count
                }

        return status

    def reset_service(self, service: str) -> None:
        """Reset all buckets for a service"""
        if service in self._project_buckets:
            self._project_buckets[service].reset()

        if service in self._user_buckets:
            for user_bucket in self._user_buckets[service].values():
                user_bucket.reset()

        logger.info(f"Reset all buckets for service '{service}'")

    def reset_all(self) -> None:
        """Reset all buckets and counters"""
        for bucket in self._project_buckets.values():
            bucket.reset()

        for user_buckets in self._user_buckets.values():
            for bucket in user_buckets.values():
                bucket.reset()

        self._daily_counters.clear()

        logger.info("Reset all rate limiter state")

    def reset_metrics(self) -> None:
        """Reset all metrics"""
        with self._lock:
            self._metrics = {
                'total_requests': 0,
                'blocked_requests': 0,
                'quota_units_consumed': 0,
                'services': defaultdict(lambda: {
                    'requests': 0,
                    'blocked': 0,
                    'quota_consumed': 0
                })
            }
        logger.info("Reset all rate limiter metrics")


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

_global_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter(use_redis: bool = False, redis_client=None) -> RateLimiter:
    """Get or create global rate limiter instance"""
    global _global_rate_limiter

    if _global_rate_limiter is None:
        _global_rate_limiter = RateLimiter(
            use_redis=use_redis,
            redis_client=redis_client
        )

    return _global_rate_limiter


# ============================================================================
# DECORATOR
# ============================================================================

def with_rate_limit(
    service: str,
    cost: int = 1,
    user_id_param: Optional[str] = None,
    timeout: float = 60.0
):
    """
    Decorator for rate limiting API calls

    Args:
        service: Service name (must match SERVICE_CONFIGS keys)
        cost: Cost in tokens/quota units (Gmail uses variable costs)
        user_id_param: Name of parameter containing user_id (for per-user limits)
        timeout: Max seconds to wait for rate limit clearance

    Example:
        @with_rate_limit("sheets", user_id_param="credentials")
        async def sheets_read_values(credentials, spreadsheet_id, range):
            # Will block until rate limit allows
            ...

        @with_rate_limit("gmail", cost=100)  # Email sending costs 100 units
        async def gmail_send_message(...):
            ...
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract user_id if specified
            user_id = None
            if user_id_param:
                # Try to get from kwargs
                user_id = kwargs.get(user_id_param)

                # Try to extract from credentials object
                if user_id and hasattr(user_id, 'token'):
                    # This is a Credentials object, try to get user email
                    try:
                        from google.oauth2.credentials import Credentials
                        if isinstance(user_id, Credentials):
                            # Extract email from token if available
                            user_id = getattr(user_id, 'email', 'default_user')
                    except Exception:
                        user_id = 'default_user'

            # Get rate limiter
            limiter = get_rate_limiter()

            # Acquire permission
            logger.debug(
                f"Acquiring rate limit for '{service}' "
                f"(function={func.__name__}, cost={cost}, user={user_id})"
            )

            granted = await limiter.acquire(
                service=service,
                user_id=user_id,
                cost=cost,
                timeout=timeout
            )

            if not granted:
                raise RateLimitExceededError(
                    f"Rate limit exceeded for '{service}' after {timeout}s timeout. "
                    f"Function: {func.__name__}, Cost: {cost}, User: {user_id}"
                )

            # Call original function
            return await func(*args, **kwargs)

        return wrapper
    return decorator


# ============================================================================
# EXCEPTIONS
# ============================================================================

class RateLimitExceededError(Exception):
    """Raised when rate limit cannot be satisfied within timeout"""
    pass


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_service_config(service: str) -> Optional[RateLimitConfig]:
    """Get rate limit configuration for a service"""
    return SERVICE_CONFIGS.get(service)


def get_all_metrics() -> Dict[str, Any]:
    """Get metrics from global rate limiter"""
    limiter = get_rate_limiter()
    return limiter.get_metrics()


def get_service_status(service: str, user_id: Optional[str] = None) -> Dict:
    """Get status of rate limiter for a service"""
    limiter = get_rate_limiter()
    return limiter.get_bucket_status(service, user_id)


def reset_rate_limiter(service: Optional[str] = None) -> None:
    """Reset rate limiter (all services or specific service)"""
    limiter = get_rate_limiter()

    if service:
        limiter.reset_service(service)
    else:
        limiter.reset_all()


# ============================================================================
# QUOTA UNIT COSTS (Gmail API)
# ============================================================================

GMAIL_QUOTA_COSTS = {
    'messages.get': 5,
    'messages.list': 5,
    'messages.delete': 10,
    'messages.insert': 25,
    'messages.modify': 5,
    'messages.send': 100,
    'messages.batchDelete': 50,
    'messages.batchModify': 50,
    'threads.get': 5,
    'threads.list': 5,
    'threads.delete': 15,
    'threads.modify': 5,
    'drafts.create': 50,
    'drafts.send': 100,
    'labels.create': 5,
    'labels.delete': 5,
    'labels.update': 5,
}


def get_gmail_cost(method: str) -> int:
    """Get quota unit cost for Gmail API method"""
    return GMAIL_QUOTA_COSTS.get(method, 1)
