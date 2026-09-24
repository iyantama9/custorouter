"""Durable, batched persistence for request telemetry.

Inference handlers only append a small non-sensitive record to Redis. This
worker reads it with a consumer group and acknowledges it only after Postgres
has accepted the whole batch. Redis Streams therefore absorb short database
slowdowns without creating an unbounded asyncio task per inference request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from typing import Any

from redis.exceptions import RedisError

from app.database import persist_request_log_batch
from app.redis_store import (
    ack_request_log_entries,
    claim_stale_request_log_batch,
    ensure_request_log_consumer_group,
    read_request_log_batch,
)


logger = logging.getLogger(__name__)
_BATCH_SIZE = max(10, min(500, int(os.getenv("REQUEST_LOG_BATCH_SIZE", "100"))))
_RECLAIM_INTERVAL_SECONDS = max(5, int(os.getenv("REQUEST_LOG_RECLAIM_INTERVAL_SECONDS", "30")))


def _consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _decode(entries: list[tuple[str, dict[str, str]]]) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    entry_ids: list[str] = []
    for entry_id, fields in entries:
        try:
            payload = json.loads(fields["payload"])
            row = {
                "event_id": entry_id,
                "model": str(payload["model"]),
                "status_code": int(payload["status_code"]),
                "key_prefix": str(payload["key_prefix"]),
                "rotated": bool(payload["rotated"]),
                "latency_ms": int(payload["latency_ms"]),
                "input_tokens": int(payload.get("input_tokens", 0)),
                "output_tokens": int(payload.get("output_tokens", 0)),
                "cached_tokens": int(payload.get("cached_tokens", 0)),
                "provider": str(payload.get("provider") or ""),
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            # A malformed record must not poison the queue forever. It contains
            # no client content, only telemetry, so discard it after logging.
            logger.warning("Discarding malformed request-log stream entry %s", entry_id)
            entry_ids.append(entry_id)
            continue
        rows.append(row)
        entry_ids.append(entry_id)
    return rows, entry_ids


async def run_request_log_worker() -> None:
    consumer = _consumer_name()
    await ensure_request_log_consumer_group()
    next_reclaim = 0.0
    while True:
        now = asyncio.get_running_loop().time()
        try:
            entries: list[tuple[str, dict[str, str]]] = []
            if now >= next_reclaim:
                entries = await claim_stale_request_log_batch(consumer, _BATCH_SIZE)
                next_reclaim = now + _RECLAIM_INTERVAL_SECONDS
            if not entries:
                reads = await read_request_log_batch(consumer, _BATCH_SIZE)
                for _, batch in reads:
                    entries.extend(batch)
            if not entries:
                continue
            rows, entry_ids = _decode(entries)
            if rows:
                await persist_request_log_batch(rows)
            await ack_request_log_entries(entry_ids)
        except asyncio.CancelledError:
            raise
        except (RedisError, RuntimeError, OSError) as exc:
            logger.warning("Request-log worker retrying after backend error: %s", type(exc).__name__)
            await asyncio.sleep(1)
        except Exception:
            # Keep durable entries pending for another attempt. The exception
            # is logged with traceback for diagnostics without affecting model
            # responses that already completed.
            logger.exception("Request-log worker failed to persist a batch")
            await asyncio.sleep(1)
