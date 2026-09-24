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
from redis.exceptions import RedisError, ResponseError


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "llm-router")
LIVE_LOG_LIMIT = max(25, int(os.getenv("REDIS_LIVE_LOG_LIMIT", "250")))
LIVE_LOG_TTL_SECONDS = max(60, int(os.getenv("REDIS_LIVE_LOG_TTL_SECONDS", "86400")))
REQUEST_LOG_STREAM_MAXLEN = max(1_000, int(os.getenv("REDIS_REQUEST_LOG_STREAM_MAXLEN", "100000")))
REQUEST_LOG_CONSUMER_GROUP = "request-log-writers-v1"

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


async def cache_live_log(log_item: dict[str, Any]) -> None:
    """Keep a bounded, short-lived copy of the dashboard's newest logs.

    PostgreSQL remains the durable source for search and historical pages. This
    list only removes a hot-path query when an operator opens the default
    newest-first Activity page, and is deliberately capped/expired so Redis
    cannot turn request logging into unbounded memory growth.
    """
    encoded = json.dumps(log_item, separators=(",", ":"))
    key = _key("dashboard:live-logs")
    pipe = _redis().pipeline(transaction=True)
    pipe.lpush(key, encoded)
    pipe.ltrim(key, 0, LIVE_LOG_LIMIT - 1)
    pipe.expire(key, LIVE_LOG_TTL_SECONDS)
    await pipe.execute()


async def get_live_logs(limit: int) -> list[dict[str, Any]]:
    """Return the recent dashboard log ring-buffer, newest first."""
    if limit < 1:
        return []
    raw_items = await _redis().lrange(_key("dashboard:live-logs"), 0, limit - 1)
    result: list[dict[str, Any]] = []
    for raw in raw_items:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


async def publish_dashboard_event(event_type: str, payload: dict[str, Any], origin: str) -> None:
    """Publish a dashboard-only event to sibling router processes.

    The origin marker lets the publishing process fan out locally immediately
    without sending every browser duplicate SSE events when it receives its
    own Redis Pub/Sub message back.
    """
    envelope = {
        "origin": origin,
        "event": {"type": event_type, "payload": payload},
    }
    await _redis().publish(
        _key("dashboard:events"),
        json.dumps(envelope, separators=(",", ":")),
    )


async def open_dashboard_event_subscription():
    """Create an independent Pub/Sub connection for SSE fan-out."""
    pubsub = _redis().pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(_key("dashboard:events"))
    return pubsub


async def redis_available() -> bool:
    try:
        return bool(await _redis().ping())
    except (RedisError, RuntimeError):
        return False


def _request_log_stream_key() -> str:
    return _key("request-logs:v1")


async def ensure_request_log_consumer_group() -> None:
    """Create the durable request-log consumer group once per deployment."""
    try:
        await _redis().xgroup_create(
            _request_log_stream_key(), REQUEST_LOG_CONSUMER_GROUP, id="0-0", mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def enqueue_request_log(payload: dict[str, Any]) -> str:
    """Append a non-sensitive request-log payload without waiting for Postgres."""
    return await _redis().xadd(
        _request_log_stream_key(),
        {"payload": json.dumps(payload, separators=(",", ":"))},
        maxlen=REQUEST_LOG_STREAM_MAXLEN,
        approximate=True,
    )


async def read_request_log_batch(consumer: str, count: int, block_ms: int = 1000):
    entries = await _redis().xreadgroup(
        REQUEST_LOG_CONSUMER_GROUP,
        consumer,
        {_request_log_stream_key(): ">"},
        count=count,
        block=block_ms,
    )
    return entries or []


async def claim_stale_request_log_batch(consumer: str, count: int, min_idle_ms: int = 60_000):
    """Take over unacknowledged entries from a stopped writer process."""
    result = await _redis().xautoclaim(
        _request_log_stream_key(),
        REQUEST_LOG_CONSUMER_GROUP,
        consumer,
        min_idle_ms,
        "0-0",
        count=count,
    )
    # redis-py returns (next_start_id, [(id, fields)], deleted_ids).
    return result[1] if result and len(result) > 1 else []


async def ack_request_log_entries(entry_ids: list[str]) -> None:
    if not entry_ids:
        return
    stream = _request_log_stream_key()
    pipe = _redis().pipeline(transaction=True)
    pipe.xack(stream, REQUEST_LOG_CONSUMER_GROUP, *entry_ids)
    pipe.xdel(stream, *entry_ids)
    await pipe.execute()
