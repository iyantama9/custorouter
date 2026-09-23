import asyncio
import json
import logging
import secrets

from redis.exceptions import RedisError

from app.redis_store import (
    cache_live_log,
    open_dashboard_event_subscription,
    publish_dashboard_event,
)


logger = logging.getLogger(__name__)


class SSEBroadcaster:
    def __init__(self):
        self._queues: set = set()
        self._origin = secrets.token_urlsafe(12)
        self._redis_task: asyncio.Task | None = None
        self._redis_pubsub = None
        self._publish_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _encode(event_type: str, payload: dict) -> str:
        # Keep the wire representation stable for existing SSE consumers.
        return json.dumps({"type": event_type, "payload": payload})

    def _fanout(self, data: str) -> None:
        """Put a pre-encoded event on local browser queues without blocking."""
        dead = set()
        for q in self._queues:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                # Status/log events are snapshots; keep the newest data rather
                # than disconnecting a client and leaving it waiting forever.
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    dead.add(q)
        for q in dead:
            self.disconnect(q)

    async def start(self) -> None:
        """Subscribe once so all application workers share live dashboard SSE."""
        if self._redis_task is None or self._redis_task.done():
            self._redis_task = asyncio.create_task(self._listen_redis_events())

    async def stop(self) -> None:
        if self._redis_task is not None:
            self._redis_task.cancel()
            await asyncio.gather(self._redis_task, return_exceptions=True)
            self._redis_task = None
        if self._redis_pubsub is not None:
            try:
                await self._redis_pubsub.aclose()
            except RedisError:
                pass
            self._redis_pubsub = None
        if self._publish_tasks:
            await asyncio.gather(*self._publish_tasks, return_exceptions=True)

    async def _listen_redis_events(self) -> None:
        """Relay Redis Pub/Sub events to only this process's SSE clients."""
        reconnect_delay = 0.5
        while True:
            try:
                self._redis_pubsub = await open_dashboard_event_subscription()
                reconnect_delay = 0.5
                # Do not use PubSub.listen() here: it waits indefinitely for a
                # frame and collides with the client's normal 3-second socket
                # timeout. A short get_message timeout keeps an idle dashboard
                # subscription healthy without a reconnect loop.
                while True:
                    message = await self._redis_pubsub.get_message(timeout=1.0)
                    if message is None:
                        continue
                    if message.get("type") != "message":
                        continue
                    try:
                        envelope = json.loads(message["data"])
                        event = envelope["event"]
                        if envelope.get("origin") != self._origin and isinstance(event, dict):
                            self._fanout(self._encode(event["type"], event["payload"]))
                    except (KeyError, TypeError, json.JSONDecodeError):
                        logger.warning("Ignoring malformed dashboard event from Redis")
            except asyncio.CancelledError:
                raise
            except (RedisError, RuntimeError) as exc:
                # A Redis reconnect must restore cross-worker events without
                # requiring a dashboard/app restart. Local SSE keeps working
                # while this isolated listener backs off.
                logger.warning("Redis dashboard event listener reconnecting: %s", exc)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 10)
            finally:
                if self._redis_pubsub is not None:
                    try:
                        await self._redis_pubsub.aclose()
                    except RedisError:
                        pass
                    self._redis_pubsub = None

    def _publish_later(self, event_type: str, payload: dict) -> None:
        """Do not add a Redis round trip to an inference response's tail."""
        if self._redis_task is None or self._redis_task.done():
            return

        async def publish():
            try:
                if event_type == "log":
                    await cache_live_log(payload)
                await publish_dashboard_event(event_type, payload, self._origin)
            except (RedisError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning("Could not publish dashboard event to Redis: %s", exc)

        task = asyncio.create_task(publish())
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    def connect(self) -> asyncio.Queue:
        # Bound per-dashboard buffering so a suspended browser tab cannot grow
        # server memory indefinitely under heavy router traffic.
        q = asyncio.Queue(maxsize=100)
        self._queues.add(q)
        return q

    def disconnect(self, q: asyncio.Queue):
        self._queues.discard(q)

    async def broadcast(self, event_type: str, payload: dict):
        self._fanout(self._encode(event_type, payload))
        self._publish_later(event_type, payload)


sse_broadcaster = SSEBroadcaster()
