"""Redis-backed shared state for the router.

Only hashes or non-sensitive dashboard payloads are stored here.  Redis is
required in production: using a process-local fallback for authentication
would make a restart silently invalidate sessions again.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import redis.asyncio as redis
from redis.exceptions import RedisError


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "llm-router")

_client: redis.Redis | None = None


def _key(name: str) -> str:
    return f"{REDIS_KEY_PREFIX}:{name}"


async def init_redis() -> None:
    """Connect eagerly so an app marked healthy always has shared auth state."""
    global _client
    if _client is None:
        _client = redis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            health_check_interval=30,
        )
    await _client.ping()


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _redis() -> redis.Redis:
    if _client is None:
        raise RuntimeError("Redis is not initialized")
    return _client


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(token: str, ttl_seconds: int) -> None:
    await _redis().set(_key(f"session:{token_digest(token)}"), "1", ex=ttl_seconds)


async def validate_session(token: str | None, ttl_seconds: int) -> bool:
    if not token or len(token) > 128:
        return False
    key = _key(f"session:{token_digest(token)}")
    # EXPIRE makes the server-side TTL sliding. The cookie keeps its own
    # bounded lifetime, so an abandoned browser cannot remain logged in.
    pipe = _redis().pipeline(transaction=True)
    pipe.exists(key)
    pipe.expire(key, ttl_seconds)
    exists, _ = await pipe.execute()
    return bool(exists)


async def revoke_session(token: str | None) -> None:
    if token:
        await _redis().delete(_key(f"session:{token_digest(token)}"))


async def consume_login_attempt(ip: str, *, per_ip_limit: int, per_ip_window: int,
                                global_limit: int, global_window: int) -> bool:
    """Atomically count a login attempt and enforce both limiter buckets."""
    script = """
local per_ip = redis.call('INCR', KEYS[1])
if per_ip == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local global = redis.call('INCR', KEYS[2])
if global == 1 then redis.call('EXPIRE', KEYS[2], ARGV[2]) end
if per_ip > tonumber(ARGV[3]) or global > tonumber(ARGV[4]) then return 0 end
return 1
"""
    result = await _redis().eval(
        script,
        2,
        _key(f"login:ip:{ip}"),
        _key("login:global"),
        per_ip_window,
        global_window,
        per_ip_limit,
        global_limit,
    )
    return result == 1


async def clear_login_attempts(ip: str) -> None:
    await _redis().delete(_key(f"login:ip:{ip}"))


async def get_cached_json(name: str) -> Any | None:
    raw = await _redis().get(_key(f"cache:{name}"))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def set_cached_json(name: str, value: Any, ttl_seconds: int) -> None:
    await _redis().set(_key(f"cache:{name}"), json.dumps(value, separators=(",", ":")), ex=ttl_seconds)


async def redis_available() -> bool:
    try:
        return bool(await _redis().ping())
    except (RedisError, RuntimeError):
        return False
