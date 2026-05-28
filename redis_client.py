"""
Shared Redis client. Centralized so URL validation happens in one place.

If REDIS_URL is missing, empty, or malformed, falls back to localhost so
the bot can still boot (degraded — Redis-dependent features won't persist).
"""

import os
import logging
import redis

log = logging.getLogger(__name__)

VALID_SCHEMES = ("redis://", "rediss://", "unix://")


def _resolve_url() -> str:
    raw = (os.environ.get("REDIS_URL") or "").strip()
    if not raw:
        log.warning("REDIS_URL not set — falling back to redis://localhost:6379 (persistence will fail)")
        return "redis://localhost:6379"
    if not raw.startswith(VALID_SCHEMES):
        log.warning(
            f"REDIS_URL has invalid scheme (got '{raw[:20]}...'). "
            f"Expected one of {VALID_SCHEMES}. Falling back to localhost."
        )
        return "redis://localhost:6379"
    return raw


def get_redis():
    """Return a Redis client. Never raises at module-load time."""
    url = _resolve_url()
    try:
        return redis.from_url(url, decode_responses=True, ssl_cert_reqs=None)
    except Exception as e:
        log.warning(f"Redis client init failed ({e}); using localhost fallback")
        return redis.from_url("redis://localhost:6379", decode_responses=True, ssl_cert_reqs=None)
